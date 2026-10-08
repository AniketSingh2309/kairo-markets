"""Number extraction, grounding and citation checks."""

from __future__ import annotations

import datetime as dt

import pytest

from core.facts import FactRegistry
from core.schemas import Claim, SummaryOutput
from core.validation import Grounder, extract_numbers, strip_citations, validate_summary

OBS = dt.datetime(2026, 9, 29, 20, tzinfo=dt.timezone.utc)


@pytest.fixture
def registry() -> FactRegistry:
    reg = FactRegistry()
    reg.add("P", kind="reported", label="Close price on 2026-09-29 (quote feed)", value=246.10,
            unit="USD", display="246.10 USD", source_id="mock://q", observed_at=OBS)
    reg.add("P", kind="reported", label="Volume on 2026-09-29", value=50870000, unit="shares",
            display="50,870,000 shares", source_id="mock://q", observed_at=OBS)
    reg.add("C", kind="computed", label="Close-to-close change 2026-08-31 to 2026-09-29",
            value=7.28, unit="%", display="+7.28%", source_id="mock://h", observed_at=OBS)
    reg.add("N", kind="reported", label="Announcement on 2026-09-18 (corporate)",
            value="Apple schedules Q4 FY2026 earnings call for October 29, 2026",
            display="Apple schedules Q4 FY2026 earnings call for October 29, 2026",
            source_id="mock://a/1", observed_at=OBS)
    return reg


@pytest.mark.parametrize("text,expected", [
    ("closed at $246.10", [246.10]),
    ("up 7%", [7.0]),
    ("fell -3.5% on the day", [3.5]),
    ("volume of 1,234,567 shares", [1234567.0]),
    ("about 50.9 million shares", [50_900_000.0]),
    ("roughly 51M shares", [51_000_000.0]),
    ("about 38.4 M shares", [38_400_000.0]),
    ("could sell 6 M units", [6_000_000.0]),  # narrow no-break space, as gpt-oss writes it
    ("Q4 FY2026 results", []),
])
def test_extract_numbers(text, expected):
    got = [abs(n.scaled) for n in extract_numbers(text)]
    assert got == pytest.approx(expected)


def test_citation_markers_are_not_numbers():
    assert extract_numbers(strip_citations("The close was higher [P1][C12].")) == []


def test_number_in_cited_fact_is_grounded(registry):
    check = Grounder(registry).check("The stock closed at 246.10 USD.", ["P1"])
    assert check.ok and check.citations == ["P1"] and not check.fixes


@pytest.mark.parametrize("text", ["It closed near 246.", "Shares rose about 7% over the month.",
                                  "Volume was roughly 50.9 million shares."])
def test_rounded_numbers_are_grounded(registry, text):
    assert Grounder(registry).check(text, ["P1", "P2", "C1"]).ok


def test_number_from_uncited_fact_is_auto_cited(registry):
    check = Grounder(registry).check("It closed at 246.10 and rose 7.28%.", ["P1"])
    assert check.ok
    assert check.citations == ["P1", "C1"]
    assert any("C1" in fix for fix in check.fixes)


def test_fabricated_number_is_a_problem(registry):
    check = Grounder(registry).check("Analysts expect a target of 300 USD.", ["P1"])
    assert not check.ok
    assert "'300'" in check.problems[0]


def test_imprecise_number_is_not_grounded(registry):
    # 246.5 does not round-match 246.10 at one decimal place.
    assert not Grounder(registry).check("It closed at 246.5.", ["P1"]).ok


def test_unknown_citations_are_dropped_and_missing_citations_fail(registry):
    check = Grounder(registry).check("The close was strong.", ["P9", "P1"])
    assert check.ok and check.citations == ["P1"]
    assert not Grounder(registry).check("The close was strong.", ["P9"]).ok
    assert not Grounder(registry).check("The close was strong.", []).ok


def test_numbers_inside_announcement_titles_are_grounded(registry):
    assert Grounder(registry).check("Earnings are due on October 29, 2026.", ["N1"]).ok


def test_extra_texts_allow_code_generated_numbers(registry):
    grounder = Grounder(registry, ["announcements lag prices by 16.9 days"])
    assert grounder.check("The feeds are 16.9 days apart.", ["P1"]).ok


def test_validate_summary_removes_bad_items(registry):
    summary = SummaryOutput(
        headline="Price target of 400 looks likely",
        claims=[
            Claim(text="The latest close was 246.10 USD.", citations=["P1"]),
            Claim(text="Revenue grew 12% last quarter.", citations=["P1"]),
            Claim(text="Momentum is positive.", citations=[]),
        ],
    )
    result = validate_summary(summary, Grounder(registry))
    assert result.headline is None
    assert [c.text for c in result.claims] == ["The latest close was 246.10 USD."]
    assert {r.where for r in result.removed} == {"headline", "claim"}
    assert len(result.removed) == 3
    assert result.problems


def test_digits_embedded_in_fact_text_are_groundable():
    reg = FactRegistry()
    reg.add("C", kind="computed", label="Golden/death cross (SMA50 vs SMA200)",
            value="SMA50 321.98 / SMA200 288.33", display="SMA50 321.98 / SMA200 288.33",
            source_id="mock://h", observed_at=OBS)
    assert Grounder(reg).check("The 50-day average sits above the 200-day average.", ["C1"]).ok
    assert not Grounder(reg).check("It sits above the 100-day average.", ["C1"]).ok
