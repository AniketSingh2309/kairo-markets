"""Evaluation harness.

Runs every case in ``eval/test_set.json`` through the full pipeline and checks:

  status              - pipeline status is one of the expected values
  flags               - required data-quality flags raised, forbidden ones absent
  expected_facts      - known values from the mock source appear as facts
  facts_match_source  - every reported fact is traceable to the raw mock data (no fabricated facts)
  no_fabricated_numbers - every number in generated text exists in the returned facts
  citations           - every generated claim/signal cites >=1 fact id, and all ids resolve
  sources             - every fact's source_id is listed in the response sources
  freshness           - timestamp discrepancies surfaced (or not) as expected, notice in report
  separation          - report keeps "reported facts" and "generated interpretation" apart
  confidence / llm_calls / facts presence - case-specific expectations
  latency             - per-request latency within budget

Usage:
    python -m eval.run_eval --mode stub            # offline, deterministic agents
    python -m eval.run_eval --mode groq --sleep 6  # real Llama 3.3 70B on Groq
    python -m eval.run_eval --mode stub --case msft_timestamp_skew
Exit code is 1 if any check fails.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agents.orchestrator import Orchestrator
from core.clock import fixed_clock
from core.config import Settings
from core.schemas import CONFIDENCE_ORDER, AnalyzeRequest, AnalyzeResponse, Fact
from core.validation import extract_numbers, fact_numbers, strip_citations
from tools.providers import MockMarketDataProvider

EVAL_DIR = Path(__file__).resolve().parent
DEFAULT_TEST_SET = EVAL_DIR / "test_set.json"
RESULTS_DIR = EVAL_DIR / "results"
DEFAULT_LATENCY_BUDGET_MS = {"stub": 6_000, "groq": 30_000}


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class CaseResult:
    case_id: str
    status: str
    latency_ms: float
    tool_calls: int
    llm_calls: int
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)


# ---------------------------------------------------------------------------
# Ground truth helpers (read the raw mock file directly, bypassing the pipeline)
# ---------------------------------------------------------------------------


def _raw_numbers(obj: Any) -> list[float]:
    if isinstance(obj, bool):
        return []
    if isinstance(obj, (int, float)):
        return [float(obj)]
    if isinstance(obj, dict):
        return [n for v in obj.values() for n in _raw_numbers(v)]
    if isinstance(obj, list):
        return [n for v in obj for n in _raw_numbers(v)]
    return []


def _raw_strings(obj: Any) -> set[str]:
    if isinstance(obj, str):
        return {obj}
    if isinstance(obj, dict):
        return {s for v in obj.values() for s in _raw_strings(v)}
    if isinstance(obj, list):
        return {s for v in obj for s in _raw_strings(v)}
    return set()


def _values_equal(actual: Any, expected: Any) -> bool:
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return abs(float(actual) - float(expected)) <= 0.005
    return actual == expected


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _generated_texts(resp: AnalyzeResponse) -> list[str]:
    if resp.answer is None:
        return []
    a = resp.answer
    return ([a.headline] + [c.text for c in a.claims]
            + [f"{s.label}. {s.rationale}" for s in a.signals] + list(a.caveats))


def check_case(case: dict[str, Any], resp: AnalyzeResponse, truth: MockMarketDataProvider,
               latency_budget_ms: float) -> list[Check]:
    checks: list[Check] = []
    facts: list[Fact] = resp.reported_facts + resp.computed_metrics
    fact_ids = {f.id for f in facts}
    codes = {f.code for f in resp.flags}

    checks.append(Check("status", resp.status in case["expect_status"],
                        f"got {resp.status}, expected one of {case['expect_status']}"))

    missing = [c for c in case.get("required_flags", []) if c not in codes]
    present = [c for c in case.get("forbidden_flags", []) if c in codes]
    checks.append(Check("flags", not missing and not present,
                        f"missing={missing} forbidden_present={present} all={sorted(codes)}"))

    for exp in case.get("expected_facts", []):
        match = next((f for f in facts if exp["label_contains"] in f.label
                      and _values_equal(f.value, exp["value"])), None)
        checks.append(Check(f"fact:{exp['label_contains']}", match is not None,
                            f"expected {exp['value']!r}" + ("" if match else " - not found")))

    # Reported facts must be traceable to the raw source record.
    symbol = resp.symbol
    if symbol in truth.symbols:
        record = truth.raw_record(symbol)
        raw_nums, raw_strs = _raw_numbers(record), _raw_strings(record)
        untraceable = [
            f.id for f in resp.reported_facts
            if (isinstance(f.value, str) and f.value not in raw_strs)
            or (not isinstance(f.value, str) and not any(abs(float(f.value) - n) < 1e-6 for n in raw_nums))
        ]
        checks.append(Check("facts_match_source", not untraceable,
                            f"untraceable reported facts: {untraceable}" if untraceable else "all traceable"))
    elif resp.reported_facts:
        checks.append(Check("facts_match_source", False, "facts returned for a symbol absent from source"))

    # Every number in generated text must exist in the returned facts (or code-written notices).
    allowed = [n for f in facts for n in fact_numbers(f)]
    notice = (resp.freshness.notice or "") if resp.freshness else ""
    extra_text = " ".join([resp.question, notice, *(f.message for f in resp.flags)])
    allowed += [n.scaled for n in extract_numbers(extra_text)]
    fabricated = [
        n.raw for text in _generated_texts(resp) for n in extract_numbers(strip_citations(text))
        if not any(n.matches(a) for a in allowed)
    ]
    checks.append(Check("no_fabricated_numbers", not fabricated,
                        f"ungrounded: {fabricated}" if fabricated else "all numbers grounded"))

    if resp.answer is not None:
        bad = [c.text[:50] for c in resp.answer.claims
               if not c.citations or not set(c.citations) <= fact_ids]
        bad += [s.label[:50] for s in resp.answer.signals
                if not s.citations or not set(s.citations) <= fact_ids]
        checks.append(Check("citations", not bad and bool(resp.answer.claims),
                            f"uncited/invalid: {bad}" if bad else f"{len(resp.answer.claims)} claims cited"))
    if case.get("expect_answer") is not None:
        checks.append(Check("answer_presence", (resp.answer is not None) == case["expect_answer"],
                            f"answer present={resp.answer is not None}"))

    source_ids = {s.source_id for s in resp.sources}
    unsourced = [f.id for f in facts if f.source_id not in source_ids]
    checks.append(Check("sources", not unsourced and (bool(source_ids) or not facts),
                        f"facts without listed source: {unsourced}" if unsourced else f"{len(source_ids)} sources"))

    discrepancy = bool(resp.freshness and resp.freshness.discrepancy)
    fresh_ok = discrepancy == case.get("expect_discrepancy", False)
    if discrepancy:
        fresh_ok = fresh_ok and resp.freshness.notice in resp.report_markdown  # type: ignore[union-attr, operator]
    if facts:
        fresh_ok = fresh_ok and resp.freshness is not None and bool(resp.freshness.datasets) \
            and "## Data freshness" in resp.report_markdown
    checks.append(Check("freshness", fresh_ok,
                        f"discrepancy={discrepancy} expected={case.get('expect_discrepancy', False)}"))

    if resp.answer is not None:
        sep = ("## Reported facts (from source data)" in resp.report_markdown
               and "## Answer (generated interpretation)" in resp.report_markdown)
        checks.append(Check("separation", sep, "facts and interpretation in separate sections"))

    if "max_confidence" in case and resp.answer is not None:
        ok = CONFIDENCE_ORDER[resp.answer.confidence] <= CONFIDENCE_ORDER[case["max_confidence"]]
        checks.append(Check("confidence_cap", ok,
                            f"confidence={resp.answer.confidence} max={case['max_confidence']}"))
    if case.get("expect_no_llm_calls"):
        checks.append(Check("no_llm_calls", resp.metrics.llm_calls == 0,
                            f"llm_calls={resp.metrics.llm_calls}"))
    if case.get("expect_no_facts"):
        checks.append(Check("no_facts", not facts, f"{len(facts)} facts"))
    if case.get("expect_no_price_facts"):
        price = [f.id for f in facts if f.id.startswith("P")]
        checks.append(Check("no_price_facts", not price, f"price facts: {price}"))

    if case.get("expect_forecast") is not None:
        checks.append(Check("forecast_presence", (resp.forecast is not None) == case["expect_forecast"],
                            f"forecast present={resp.forecast is not None}"))
    if resp.forecast is not None and "max_forecast_confidence" in case:
        ok = CONFIDENCE_ORDER[resp.forecast.confidence] <= CONFIDENCE_ORDER[case["max_forecast_confidence"]]
        checks.append(Check("forecast_confidence", ok, f"confidence={resp.forecast.confidence} "
                            f"({resp.forecast.confidence_reason})"))
    if resp.forecast is not None and resp.answer is not None:
        # Any statement of the outlook must be accompanied by the model's measured hit rate.
        outlook = next((f.id for f in facts if f.label.startswith("Statistical ")), None)
        # The hit-rate fact or the confidence fact (whose detail states the hit rates) must be cited too.
        disclosure = {f.id for f in facts if f.label.startswith("Backtested hit rate of the forecast model")
                      or f.label == "Forecast confidence"}
        undisclosed = [c.text[:60] for c in resp.answer.claims
                       if outlook in c.citations and disclosure and not disclosure & set(c.citations)]
        checks.append(Check("forecast_disclosure", not undisclosed,
                            f"outlook stated without backtest hit rate: {undisclosed}" if undisclosed
                            else "outlook always stated with its hit rate"))

    checks.append(Check("tool_budget", resp.metrics.tool_calls <= resp.metrics.tool_call_budget,
                        f"{resp.metrics.tool_calls}/{resp.metrics.tool_call_budget}"))
    checks.append(Check("latency", resp.metrics.latency_ms <= latency_budget_ms,
                        f"{resp.metrics.latency_ms:.0f} ms (budget {latency_budget_ms:.0f})"))
    return checks


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run(mode: str, test_set: Path, only: list[str] | None, sleep_s: float,
        latency_budget_ms: float | None) -> tuple[list[CaseResult], Path]:
    spec = json.loads(test_set.read_text(encoding="utf-8"))
    reference = dt.datetime.fromisoformat(spec["reference_time"].replace("Z", "+00:00"))
    settings = Settings.from_env().with_overrides(
        llm_provider=mode,
        data_provider="mock",
        tool_timeout_seconds=1.0,  # SLOW sleeps 5 s; keep the timeout case quick
        tool_backoff_base_seconds=0.1,
    )
    if mode == "groq" and settings.groq_api_key is None:
        sys.exit("GROQ_API_KEY is not set; use --mode stub or add it to .env")

    truth = MockMarketDataProvider(settings.mock_data_path)
    orchestrator = Orchestrator(settings, provider=MockMarketDataProvider(settings.mock_data_path),
                                clock=fixed_clock(reference))
    budget = latency_budget_ms or DEFAULT_LATENCY_BUDGET_MS[mode]

    results: list[CaseResult] = []
    responses: dict[str, Any] = {}
    cases = [c for c in spec["cases"] if not only or c["id"] in only]
    for i, case in enumerate(cases):
        if i and sleep_s:
            time.sleep(sleep_s)  # stay under Groq free-tier rate limits
        resp = orchestrator.run(AnalyzeRequest(**case["request"]))
        result = CaseResult(case_id=case["id"], status=resp.status, latency_ms=resp.metrics.latency_ms,
                            tool_calls=resp.metrics.tool_calls, llm_calls=resp.metrics.llm_calls,
                            checks=check_case(case, resp, truth, budget))
        results.append(result)
        responses[case["id"]] = resp.model_dump(mode="json")
        mark = "PASS" if result.passed else "FAIL"
        print(f"[{mark}] {case['id']:<26} status={resp.status:<17} "
              f"latency={resp.metrics.latency_ms:>7.0f}ms tools={resp.metrics.tool_calls} "
              f"llm={resp.metrics.llm_calls}")
        for check in result.checks:
            if not check.passed:
                print(f"         x {check.name}: {check.detail}")

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = RESULTS_DIR / f"eval_{mode}_{stamp}.json"
    out_path.write_text(json.dumps({
        "mode": mode,
        "model": settings.groq_model if mode == "groq" else "stub",
        "reference_time": spec["reference_time"],
        "summary": _summary(results),
        "cases": [{**asdict(r), "passed": r.passed} for r in results],
        "responses": responses,
    }, indent=2), encoding="utf-8")
    return results, out_path


def _summary(results: list[CaseResult]) -> dict[str, Any]:
    latencies = sorted(r.latency_ms for r in results)
    by_check: dict[str, list[bool]] = {}
    for r in results:
        for c in r.checks:
            by_check.setdefault(c.name.split(":")[0], []).append(c.passed)
    return {
        "cases": len(results),
        "cases_passed": sum(r.passed for r in results),
        "checks_passed": sum(c.passed for r in results for c in r.checks),
        "checks_total": sum(len(r.checks) for r in results),
        "latency_ms_mean": round(statistics.fmean(latencies), 1) if latencies else None,
        "latency_ms_p95": latencies[max(0, round(0.95 * len(latencies)) - 1)] if latencies else None,
        "pass_rate_by_check": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_check.items())},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["stub", "groq"], default="stub")
    parser.add_argument("--test-set", type=Path, default=DEFAULT_TEST_SET)
    parser.add_argument("--case", action="append", dest="cases", help="run only this case id (repeatable)")
    parser.add_argument("--sleep", type=float, default=None,
                        help="seconds between cases (default 0 for stub, 6 for groq)")
    parser.add_argument("--latency-budget-ms", type=float, default=None)
    args = parser.parse_args(argv)

    sleep_s = args.sleep if args.sleep is not None else (6.0 if args.mode == "groq" else 0.0)
    results, out_path = run(args.mode, args.test_set, args.cases, sleep_s, args.latency_budget_ms)
    summary = _summary(results)
    print("\n" + "=" * 72)
    print(f"cases passed : {summary['cases_passed']}/{summary['cases']}")
    print(f"checks passed: {summary['checks_passed']}/{summary['checks_total']}")
    print(f"latency      : mean {summary['latency_ms_mean']} ms, p95 {summary['latency_ms_p95']} ms")
    for name, rate in summary["pass_rate_by_check"].items():
        print(f"  {name:<22} {rate}")
    print(f"results      : {out_path}")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
