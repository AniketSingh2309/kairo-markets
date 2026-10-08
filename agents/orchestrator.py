"""Code-based orchestrator.

The execution order is fixed here, in Python, and every stage is recorded in
``response.trace`` with its status, duration and -- when a stage is skipped --
the stop condition that caused it. No model decides routing.

    fetch_data -> build_facts -> assess_freshness -> analyze -> validate_analysis
               -> summarize -> validate_summary [-> repair_summary -> validate_summary]
               -> finalize -> render

Stop conditions
  * fatal tool error (unknown symbol / invalid input)  -> status=error, no LLM calls
  * no dataset retrieved at all                         -> status=insufficient_data, no LLM calls
  * LLM unavailable / unparseable                       -> status=partial, facts only
  * summary fails validation                            -> ONE repair attempt, then offending
                                                           claims are dropped (never kept)
  * tool-call budget exhausted                          -> remaining calls refused, flagged
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol

from agents.analysis_agent import AnalysisAgent
from agents.data_agent import DataFetchAgent
from agents.llm import LLMError, LLMUsage
from agents.prompts import AgentContext
from agents.stubs import StubAnalysisAgent, StubSummaryAgent
from agents.summary_agent import SummaryAgent
from core.bundle import DATASETS, DataBundle
from core.clock import Clock, utc_now
from core.config import Settings, get_settings
from core.facts import build_facts
from core.forecast import MIN_BARS as MIN_FORECAST_BARS
from core.forecast import add_forecast_facts, build_forecast
from core.freshness import assess_freshness
from core.report import render_markdown
from core.schemas import (
    CONFIDENCE_ORDER,
    AnalysisOutput,
    AnalyzeRequest,
    AnalyzeResponse,
    Flag,
    GeneratedInterpretation,
    Metrics,
    StageRecord,
    ValidationReport,
)
from core.validation import Grounder, validate_analysis, validate_summary
from tools.errors import ToolError
from tools.executor import ToolExecutor
from tools.providers import MarketDataProvider, build_provider

log = logging.getLogger(__name__)

_ERROR_SEVERITY = {"UNKNOWN_SYMBOL": "critical", "INVALID_INPUT": "critical"}


class _Analyzer(Protocol):
    generated_by: str

    def run(self, ctx: AgentContext, usage: LLMUsage) -> AnalysisOutput: ...


class _Summarizer(Protocol):
    generated_by: str

    def run(self, ctx: AgentContext, analysis: AnalysisOutput, usage: LLMUsage,
            feedback: str | None = None) -> Any: ...


def create_llm_agents(settings: Settings) -> tuple[_Analyzer, _Summarizer]:
    if settings.llm_provider == "stub":
        return StubAnalysisAgent(), StubSummaryAgent()
    return AnalysisAgent(settings), SummaryAgent(settings)


class _Run:
    """Mutable per-request state; converted into an AnalyzeResponse at the end."""

    def __init__(self, request: AnalyzeRequest):
        self.request = request
        self.request_id = uuid.uuid4().hex[:12]
        self.trace: list[StageRecord] = []
        self.flags: list[Flag] = []
        self.usage = LLMUsage()
        self.t0 = time.perf_counter()

    @contextmanager
    def stage(self, name: str) -> Iterator[StageRecord]:
        record = StageRecord(stage=name, started_at=utc_now())
        t0 = time.perf_counter()
        try:
            yield record
        except Exception as exc:
            record.status = "failed"
            record.detail = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record.duration_ms = round((time.perf_counter() - t0) * 1000, 2)
            self.trace.append(record)

    def skip(self, name: str, reason: str) -> None:
        self.trace.append(StageRecord(stage=name, status="skipped", started_at=utc_now(), detail=reason))


class Orchestrator:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        provider: MarketDataProvider | None = None,
        clock: Clock = utc_now,
        data_agent: DataFetchAgent | None = None,
        analysis_agent: _Analyzer | None = None,
        summary_agent: _Summarizer | None = None,
    ):
        self.settings = settings or get_settings()
        self.provider = provider or build_provider(self.settings)
        self.clock = clock
        self.data_agent = data_agent or DataFetchAgent(self.settings)
        default_analysis, default_summary = create_llm_agents(self.settings)
        self.analysis_agent = analysis_agent or default_analysis
        self.summary_agent = summary_agent or default_summary

    # ------------------------------------------------------------------

    def run(self, request: AnalyzeRequest) -> AnalyzeResponse:
        run = _Run(request)
        reference_time = self.clock()
        executor = ToolExecutor(provider=self.provider, settings=self.settings, clock=self.clock)
        partial: dict[str, Any] = {}
        try:
            status = self._pipeline(run, executor, reference_time, partial)
        except Exception as exc:  # last-resort guard: never return a half-built answer silently
            log.exception("pipeline failed")
            run.flags.append(Flag(code="INTERNAL_ERROR", severity="critical",
                                  message=f"Pipeline error: {type(exc).__name__}: {exc}"))
            status = "error"
            partial.pop("answer", None)
        return self._finish(run, executor, status, partial)

    def _pipeline(self, run: _Run, executor: ToolExecutor, reference_time: Any,
                  out: dict[str, Any]) -> str:
        request = run.request

        # 1. Fetch -----------------------------------------------------------
        with run.stage("fetch_data") as rec:
            bundle = self.data_agent.run(request, executor, run.usage)
            rec.detail = (f"mode={bundle.mode}; ok={bundle.available()}; "
                          f"errors={ {t: e.code for t, e in bundle.errors.items()} }")
            if bundle.notes:
                rec.detail += "; notes=" + " | ".join(bundle.notes)
            if bundle.errors:
                rec.status = "degraded"
        run.flags += _flags_from_errors(bundle)
        out["sources"] = bundle.sources()

        fatal = bundle.fatal_error()
        if fatal is not None:
            for name in ("build_facts", "forecast", "assess_freshness", "analyze", "summarize"):
                run.skip(name, f"stop condition: fatal tool error {fatal.code}")
            return "error"
        if not bundle.available():
            for name in ("build_facts", "forecast", "assess_freshness", "analyze", "summarize"):
                run.skip(name, "stop condition: no dataset could be retrieved")
            return "insufficient_data"

        # 2. Facts -----------------------------------------------------------
        with run.stage("build_facts") as rec:
            registry, fact_flags = build_facts(bundle, self.settings)
            rec.detail = f"{len(registry)} facts"
        run.flags += fact_flags

        # 2b. Forecast (deterministic technical model + walk-forward backtest) --
        if bundle.history is None:
            run.skip("forecast", "no price history available")
        else:
            with run.stage("forecast") as rec:
                history = bundle.history
                forecast = build_forecast(history.data.bars, request.horizon_days, history.data.currency)
                if forecast is None:
                    rec.status = "skipped"
                    rec.detail = f"needs {MIN_FORECAST_BARS} daily bars, got {len(history.data.bars)}"
                    run.flags.append(Flag(
                        code="FORECAST_UNAVAILABLE", severity="info", dataset="price history feed",
                        message=f"No directional forecast: it needs at least {MIN_FORECAST_BARS} daily "
                                f"bars and only {len(history.data.bars)} are available."))
                else:
                    add_forecast_facts(registry, forecast, history.source.source_id, history.observed_at)
                    out["forecast"] = forecast
                    bt = forecast.backtest
                    rec.detail = (f"direction={forecast.direction}; p_up={forecast.probability_up}; "
                                  f"confidence={forecast.confidence}; backtest_hit_rate="
                                  f"{bt.models[0].hit_rate if bt else None} vs baseline "
                                  f"{bt.baseline_hit_rate if bt else None}")
        out["reported_facts"] = registry.by_kind("reported")
        out["computed_metrics"] = registry.by_kind("computed")

        # 3. Freshness -------------------------------------------------------
        with run.stage("assess_freshness") as rec:
            freshness, fresh_flags = assess_freshness(bundle, reference_time, self.settings)
            rec.detail = f"discrepancy={freshness.discrepancy}; max_skew_hours={freshness.max_skew_hours}"
        run.flags += fresh_flags
        out["freshness"] = freshness

        degraded = bool(bundle.errors)
        ctx = AgentContext(
            symbol=request.symbol,
            question=request.question,
            facts=registry.all(),
            freshness=freshness,
            flags=list(run.flags),
            missing=[f"{DATASETS[t][1]} ({e.code})" for t, e in bundle.errors.items()],
        )
        extra_texts = [request.question, freshness.notice or "",
                       *(f.message for f in run.flags),
                       request.date.isoformat() if request.date else ""]
        grounder = Grounder(registry, extra_texts)
        report = ValidationReport(passed=True, checked_items=0)

        # 4. Analyze ---------------------------------------------------------
        try:
            with run.stage("analyze") as rec:
                analysis = self.analysis_agent.run(ctx, run.usage)
                rec.detail = f"{len(analysis.signals)} signals; stance={analysis.overall_stance}"
        except LLMError as exc:
            run.flags.append(Flag(code="LLM_UNAVAILABLE", severity="warning",
                                  message=f"Analysis agent failed ({exc}); returning source facts only."))
            run.skip("summarize", "stop condition: analysis agent failed")
            return "partial"

        out["validation"] = report
        with run.stage("validate_analysis") as rec:
            checked = validate_analysis(analysis, grounder)
            analysis = checked.output
            report.checked_items += len(analysis.signals) + len(checked.removed)
            report.removed += checked.removed
            report.auto_fixes += checked.fixes
            rec.detail = f"removed={len(checked.removed)}; fixes={len(checked.fixes)}"
            if checked.removed:
                rec.status = "degraded"

        # 5. Summarize (+ one bounded repair) ---------------------------------
        try:
            with run.stage("summarize"):
                summary = self.summary_agent.run(ctx, analysis, run.usage)
            with run.stage("validate_summary") as rec:
                result = validate_summary(summary, grounder)
                rec.detail = f"removed={len(result.removed)}; fixes={len(result.fixes)}"
            if result.removed:
                report.repair_attempted = True
                feedback = "\n".join(f"- {p}" for p in result.problems)
                with run.stage("repair_summary"):
                    summary = self.summary_agent.run(ctx, analysis, run.usage, feedback=feedback)
                with run.stage("validate_summary") as rec:
                    result = validate_summary(summary, grounder)
                    rec.detail = f"after repair: removed={len(result.removed)}; fixes={len(result.fixes)}"
                    if result.removed:
                        rec.status = "degraded"
        except LLMError as exc:
            run.flags.append(Flag(code="LLM_UNAVAILABLE", severity="warning",
                                  message=f"Summary agent failed ({exc}); returning source facts only."))
            return "partial"

        report.checked_items += len(result.claims) + len(result.removed)
        report.removed += result.removed
        report.auto_fixes += result.fixes
        report.passed = not report.removed

        # 6. Finalize: code-enforced policies on top of model output ----------
        with run.stage("finalize") as rec:
            if report.removed:
                run.flags.append(Flag(
                    code="UNGROUNDED_OUTPUT_REMOVED", severity="warning",
                    message=f"{len(report.removed)} generated statement(s) were removed because they "
                            "cited no valid fact or contained numbers not present in the source data.",
                ))
            if not result.claims:
                run.flags.append(Flag(code="NO_GROUNDED_ANSWER", severity="warning",
                                      message="No generated statement survived validation; "
                                              "only source facts are returned."))
                rec.detail = "no grounded claims"
                return "partial"
            confidence = _cap_confidence(analysis.confidence, run.flags, freshness.discrepancy, degraded)
            if confidence != analysis.confidence:
                report.auto_fixes.append(
                    f"confidence capped from {analysis.confidence} to {confidence} by data-quality policy")
            headline = result.headline or f"{request.symbol}: see the cited statements below"
            out["answer"] = GeneratedInterpretation(
                generated_by=self.summary_agent.generated_by,
                headline=headline,
                claims=result.claims,
                signals=analysis.signals,
                overall_stance=analysis.overall_stance,
                confidence=confidence,
                caveats=analysis.caveats,
            )
            rec.detail = f"confidence={confidence}"

        return "partial" if degraded or report.removed else "ok"

    # ------------------------------------------------------------------

    def _finish(self, run: _Run, executor: ToolExecutor, status: str,
                out: dict[str, Any]) -> AnalyzeResponse:
        response = AnalyzeResponse(
            request_id=run.request_id,
            symbol=run.request.symbol,
            question=run.request.question,
            status=status,  # type: ignore[arg-type]
            answer=out.get("answer"),
            reported_facts=out.get("reported_facts", []),
            computed_metrics=out.get("computed_metrics", []),
            freshness=out.get("freshness"),
            forecast=out.get("forecast"),
            flags=_dedupe(run.flags),
            sources=out.get("sources", []),
            validation=out.get("validation"),
            metrics=Metrics(latency_ms=0, tool_calls=executor.budget.used,
                            tool_call_budget=executor.budget.max_calls,
                            llm_calls=run.usage.llm_calls),
            trace=run.trace,
            tool_calls=executor.ledger,
        )
        with run.stage("render"):
            response.report_markdown = render_markdown(response)
        if response.validation is not None and response.freshness is not None:
            notice = response.freshness.notice
            response.validation.freshness_notice_included = (
                notice is None or notice in response.report_markdown)
        response.metrics.latency_ms = round((time.perf_counter() - run.t0) * 1000, 2)
        response.trace = run.trace
        return response


def _flags_from_errors(bundle: DataBundle) -> list[Flag]:
    flags = []
    for tool, exc in bundle.errors.items():
        label = DATASETS[tool][1]
        flags.append(Flag(
            code=exc.code,
            severity=_ERROR_SEVERITY.get(exc.code, "warning"),  # type: ignore[arg-type]
            dataset=label,
            message=f"The {label} could not be used: {exc.message}. "
                    "It was excluded and no values were substituted.",
        ))
    return flags


def _cap_confidence(confidence: str, flags: list[Flag], discrepancy: bool, degraded: bool) -> str:
    cap = "high"
    if discrepancy or degraded or any(f.severity == "warning" for f in flags):
        cap = "medium"
    if any(f.severity == "critical" for f in flags):
        cap = "low"
    return min(confidence, cap, key=lambda c: CONFIDENCE_ORDER[c])


def _dedupe(flags: list[Flag]) -> list[Flag]:
    seen: dict[tuple[str, str | None, str], Flag] = {}
    for flag in flags:
        seen.setdefault((flag.code, flag.dataset, flag.message), flag)
    order = {"critical": 0, "warning": 1, "info": 2}
    return sorted(seen.values(), key=lambda f: order[f.severity])


__all__ = ["Orchestrator", "ToolError", "create_llm_agents"]
