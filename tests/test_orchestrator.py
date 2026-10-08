"""End-to-end pipeline with stub agents: routing, stop conditions, validation, repair."""

from __future__ import annotations

import pytest

from agents.llm import LLMUnavailableError
from agents.orchestrator import Orchestrator
from agents.stubs import StubSummaryAgent
from core.schemas import AnalyzeRequest, Claim, SummaryOutput
from tests.conftest import SlowProvider


def make(settings, clock, provider, **kwargs):
    return Orchestrator(settings, provider=provider, clock=clock, **kwargs)


def ask(orchestrator, symbol, question="How is the stock doing?", **kw):
    return orchestrator.run(AnalyzeRequest(symbol=symbol, question=question, **kw))


def stages(resp):
    return [(r.stage, r.status) for r in resp.trace]


def test_happy_path(settings, clock, mock_provider):
    resp = ask(make(settings, clock, mock_provider), "AAPL")
    assert resp.status == "ok"
    assert resp.answer is not None and resp.answer.claims
    fact_ids = {f.id for f in resp.reported_facts + resp.computed_metrics}
    for claim in resp.answer.claims:
        assert claim.citations and set(claim.citations) <= fact_ids
    assert resp.metrics.tool_calls == 3
    assert [r.stage for r in resp.trace][:4] == ["fetch_data", "build_facts", "forecast", "assess_freshness"]
    assert "## Reported facts (from source data)" in resp.report_markdown
    assert "## Answer (generated interpretation)" in resp.report_markdown
    assert resp.validation.passed


def test_unknown_symbol_stops_before_llm(settings, clock, mock_provider):
    resp = ask(make(settings, clock, mock_provider), "ZZZZ")
    assert resp.status == "error"
    assert "UNKNOWN_SYMBOL" in {f.code for f in resp.flags}
    assert resp.answer is None and not resp.reported_facts
    assert resp.metrics.llm_calls == 0
    skipped = [r for r in resp.trace if r.status == "skipped"]
    assert {r.stage for r in skipped} >= {"analyze", "summarize"}
    assert all("stop condition" in r.detail for r in skipped)


def test_all_sources_timing_out_gives_insufficient_data(settings, clock, mock_provider):
    provider = SlowProvider(mock_provider, delay=0.3)
    s = settings.with_overrides(tool_timeout_seconds=0.02)
    resp = ask(make(s, clock, provider), "AAPL")
    assert resp.status == "insufficient_data"
    assert "TOOL_TIMEOUT" in {f.code for f in resp.flags}
    assert resp.answer is None
    assert resp.metrics.tool_calls <= s.max_tool_calls_per_request


def test_malformed_prices_are_excluded_not_guessed(settings, clock, mock_provider):
    resp = ask(make(settings, clock, mock_provider), "BADF")
    assert resp.status == "partial"
    assert "MALFORMED_RESPONSE" in {f.code for f in resp.flags}
    assert not [f for f in resp.reported_facts if f.id.startswith("P")]
    assert resp.answer is not None  # announcements still usable


def test_conflicting_prices_cap_confidence(settings, clock, mock_provider):
    resp = ask(make(settings, clock, mock_provider), "NVDA")
    assert "PRICE_SOURCES_DISAGREE" in {f.code for f in resp.flags}
    assert resp.answer.confidence == "low"


def test_timestamp_discrepancy_is_in_report(settings, clock, mock_provider):
    resp = ask(make(settings, clock, mock_provider), "MSFT")
    assert resp.freshness.discrepancy
    assert resp.freshness.notice in resp.report_markdown
    assert resp.validation.freshness_notice_included
    assert resp.answer.confidence in ("low", "medium")


def test_tool_budget_exhaustion_is_flagged(settings, clock, mock_provider):
    s = settings.with_overrides(max_tool_calls_per_request=2)
    resp = ask(make(s, clock, mock_provider), "AAPL")
    assert resp.metrics.tool_calls == 2
    assert "TOOL_BUDGET_EXCEEDED" in {f.code for f in resp.flags}
    assert resp.status == "partial"


class FabricatingSummary(StubSummaryAgent):
    """Adds an invented number; optionally behaves after being given feedback."""

    def __init__(self, fix_on_repair: bool):
        self.fix_on_repair = fix_on_repair
        self.calls = 0
        self.feedback: list[str | None] = []

    def run(self, ctx, analysis, usage, feedback=None):
        self.calls += 1
        self.feedback.append(feedback)
        good = super().run(ctx, analysis, usage)
        if feedback and self.fix_on_repair:
            return good
        bad = Claim(text="Analysts see the price reaching 999.99 soon.", citations=["P1"])
        return SummaryOutput(headline=good.headline, claims=[*good.claims, bad])


def test_fabricated_claim_is_removed_after_one_repair(settings, clock, mock_provider):
    summary = FabricatingSummary(fix_on_repair=False)
    resp = ask(make(settings, clock, mock_provider, summary_agent=summary), "AAPL")
    assert summary.calls == 2  # original + exactly one repair
    assert "999.99" in summary.feedback[1]
    assert all("999.99" not in c.text for c in resp.answer.claims)
    assert resp.validation.repair_attempted and not resp.validation.passed
    assert "UNGROUNDED_OUTPUT_REMOVED" in {f.code for f in resp.flags}
    assert resp.status == "partial"


def test_successful_repair_keeps_status_ok(settings, clock, mock_provider):
    summary = FabricatingSummary(fix_on_repair=True)
    resp = ask(make(settings, clock, mock_provider, summary_agent=summary), "AAPL")
    assert summary.calls == 2
    assert resp.status == "ok"
    assert resp.validation.repair_attempted and resp.validation.passed


class FailingAnalysis:
    generated_by = "test"

    def run(self, ctx, usage):
        raise LLMUnavailableError("groq down")


def test_llm_outage_returns_facts_only(settings, clock, mock_provider):
    resp = ask(make(settings, clock, mock_provider, analysis_agent=FailingAnalysis()), "AAPL")
    assert resp.status == "partial"
    assert resp.answer is None
    assert resp.reported_facts
    assert "LLM_UNAVAILABLE" in {f.code for f in resp.flags}
    assert ("summarize", "skipped") in stages(resp)


def test_groq_mode_without_key_degrades_gracefully(settings, clock, mock_provider):
    s = settings.with_overrides(llm_provider="groq", groq_api_key=None)
    resp = ask(make(s, clock, mock_provider), "AAPL")
    assert resp.status == "partial"
    assert "LLM_UNAVAILABLE" in {f.code for f in resp.flags}
    assert resp.reported_facts


@pytest.mark.parametrize("symbol", ["AAPL", "MSFT", "TSLA", "NVDA", "BADF"])
def test_every_generated_number_is_grounded(settings, clock, mock_provider, symbol):
    from core.validation import extract_numbers, fact_numbers, strip_citations

    resp = ask(make(settings, clock, mock_provider), symbol)
    allowed = [n for f in resp.reported_facts + resp.computed_metrics for n in fact_numbers(f)]
    allowed += [n.scaled for f in resp.flags for n in extract_numbers(f.message)]
    texts = [resp.answer.headline] + [c.text for c in resp.answer.claims]
    for text in texts:
        for num in extract_numbers(strip_citations(text)):
            assert any(num.matches(a) for a in allowed), f"{num.raw} in {text!r}"
