"""Freshness assessment and fact building on real tool output from the mock source."""

from __future__ import annotations

import datetime as dt

import pytest

from agents.data_agent import DataFetchAgent
from agents.llm import LLMUsage
from core.facts import build_facts
from core.freshness import assess_freshness
from core.schemas import AnalyzeRequest
from tests.conftest import REFERENCE_TIME
from tools.executor import ToolExecutor


def fetch(symbol, settings, clock, provider, **request_kwargs):
    request = AnalyzeRequest(symbol=symbol, question="test question", **request_kwargs)
    executor = ToolExecutor(provider=provider, settings=settings, clock=clock, sleep=lambda _s: None)
    return DataFetchAgent(settings).run(request, executor, LLMUsage())


def test_aligned_data_has_no_discrepancy(settings, clock, mock_provider):
    bundle = fetch("AAPL", settings, clock, mock_provider)
    report, flags = assess_freshness(bundle, REFERENCE_TIME, settings)
    assert not report.discrepancy and report.notice is None
    assert flags == []
    assert {d.dataset for d in report.datasets} == {
        "price quote feed", "price history feed", "announcements feed"}
    assert report.max_skew_hours == pytest.approx(2.0)


def test_lagging_announcements_are_flagged_with_both_timestamps(settings, clock, mock_provider):
    bundle = fetch("MSFT", settings, clock, mock_provider)
    report, flags = assess_freshness(bundle, REFERENCE_TIME, settings)
    assert report.discrepancy
    assert "2026-09-29 20:00 UTC" in report.notice and "2026-09-12 21:00 UTC" in report.notice
    assert "not equally current" in report.notice
    codes = {(f.code, f.dataset) for f in flags}
    assert ("TIMESTAMP_DISCREPANCY", "announcements feed") in codes
    assert ("STALE_DATA", "announcements feed") in codes


def test_stale_prices_are_flagged(settings, clock, mock_provider):
    bundle = fetch("TSLA", settings, clock, mock_provider)
    report, flags = assess_freshness(bundle, REFERENCE_TIME, settings)
    stale = {d.dataset for d in report.datasets if d.stale}
    assert stale == {"price quote feed", "price history feed"}
    assert report.discrepancy


def test_requested_historical_date_is_not_stale(settings, clock, mock_provider):
    bundle = fetch("AAPL", settings, clock, mock_provider, date=dt.date(2026, 9, 15))
    report, flags = assess_freshness(bundle, REFERENCE_TIME, settings)
    quote = next(d for d in report.datasets if d.dataset == "price quote feed")
    assert quote.historical and not quote.stale
    assert not report.discrepancy and flags == []


def test_facts_carry_source_and_observed_at(settings, clock, mock_provider):
    bundle = fetch("AAPL", settings, clock, mock_provider)
    registry, flags = build_facts(bundle, settings)
    close = registry.get("P1")
    assert close.label.startswith("Close price on 2026-09-29") and close.value == pytest.approx(246.10)
    assert all(f.source_id and f.observed_at.tzinfo for f in registry.all())
    change = next(f for f in registry.all() if f.label.startswith("Close-to-close change"))
    assert change.kind == "computed" and change.value == pytest.approx(7.28) and change.method
    assert len(registry.by_kind("reported")) == 5 + 3  # bar fields + announcements
    assert flags == []


def test_conflicting_price_feeds_keep_both_values(settings, clock, mock_provider):
    bundle = fetch("NVDA", settings, clock, mock_provider)
    registry, flags = build_facts(bundle, settings)
    closes = sorted(f.value for f in registry.all() if f.label.startswith("Close price on 2026-09-29"))
    assert closes == pytest.approx([171.20, 181.40])
    flag = next(f for f in flags if f.code == "PRICE_SOURCES_DISAGREE")
    assert flag.severity == "critical" and "neither was chosen" in flag.message
