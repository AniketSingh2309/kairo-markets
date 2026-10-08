"""Pipeline-level schemas: API request/response, facts, flags, agent outputs."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field

from tools.executor import ToolCallRecord
from tools.models import Period, SourceRef, Symbol

Severity = Literal["info", "warning", "critical"]
Stance = Literal["bullish", "bearish", "neutral", "mixed", "insufficient_data"]
Confidence = Literal["low", "medium", "high"]
CONFIDENCE_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2}


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


class AnalyzeRequest(BaseModel):
    symbol: Symbol = Field(examples=["AAPL"])
    question: str = Field(min_length=3, max_length=500, examples=["How has the stock trended?"])
    date: dt.date | None = Field(None, description="Specific trading day; omit for latest")
    period: Period = Field("30d", description="Look-back window for announcements")
    horizon_days: int = Field(5, ge=1, le=20, description="Forecast horizon in trading days")


# ---------------------------------------------------------------------------
# Facts: the only numbers the system is allowed to state
# ---------------------------------------------------------------------------


class Fact(BaseModel):
    """A citable piece of data.

    kind="reported": copied verbatim from a tool response (price bar field,
    announcement). kind="computed": derived *by code* from reported data
    (moving averages, % change) -- never by the LLM.
    """

    id: str
    kind: Literal["reported", "computed"]
    label: str
    value: float | int | str
    unit: str | None = None
    display: str
    detail: str | None = None
    method: str | None = Field(None, description="How a computed fact was derived")
    source_id: str
    observed_at: AwareDatetime


class Flag(BaseModel):
    code: str
    severity: Severity
    message: str
    dataset: str | None = None


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


class DatasetFreshness(BaseModel):
    dataset: str
    source_id: str
    observed_at: AwareDatetime
    age_hours: float
    stale: bool
    historical: bool = Field(False, description="Explicitly requested past date; exempt from staleness/skew checks")


class FreshnessReport(BaseModel):
    reference_time: AwareDatetime
    datasets: list[DatasetFreshness]
    max_skew_hours: float | None = None
    discrepancy: bool = False
    notice: str | None = Field(
        None, description="Human-readable warning shown verbatim in the final answer"
    )


# ---------------------------------------------------------------------------
# Agent outputs (what the LLM must return, as JSON)
# ---------------------------------------------------------------------------


class Signal(BaseModel):
    kind: Literal["technical", "fundamental", "event"]
    label: str = Field(min_length=1, max_length=200)
    direction: Literal["bullish", "bearish", "neutral"]
    rationale: str = Field(min_length=1, max_length=600)
    citations: list[str] = Field(default_factory=list)


class AnalysisOutput(BaseModel):
    signals: list[Signal] = Field(default_factory=list, max_length=12)
    overall_stance: Stance
    confidence: Confidence
    caveats: list[str] = Field(default_factory=list, max_length=8)


class Claim(BaseModel):
    text: str = Field(min_length=1, max_length=600)
    citations: list[str] = Field(default_factory=list)


class SummaryOutput(BaseModel):
    headline: str = Field(min_length=1, max_length=200)
    claims: list[Claim] = Field(min_length=1, max_length=8)


# ---------------------------------------------------------------------------
# Forecast (deterministic statistical model -- not the LLM)
# ---------------------------------------------------------------------------


class IndicatorReading(BaseModel):
    key: str
    name: str
    category: Literal["trend", "momentum", "oscillator", "volume", "breakout", "pattern"]
    value: str = Field(description="Current indicator value, formatted")
    reading: str = Field(description="What the value means, in words")
    vote: float = Field(ge=-1, le=1, description="-1 bearish .. +1 bullish, as the textbook reads it")
    weight: float | None = Field(None, description="Adaptive weight learned from this stock's history; "
                                                   "negative = the signal has worked in reverse here")
    hit_rate: float | None = Field(None, description="Historical directional hit rate of this signal")
    samples: int = 0


class ModelBacktest(BaseModel):
    model: str
    predictions: int
    hit_rate: float | None
    ci_low: float | None = Field(None, description="95% Wilson interval, lower bound")
    ci_high: float | None = None


class BacktestReport(BaseModel):
    method: str
    horizon_days: int
    first_test_date: dt.date | None
    last_test_date: dt.date | None
    models: list[ModelBacktest]
    baseline_hit_rate: float | None = Field(
        None, description="Walk-forward 'always predict the majority direction seen so far'")
    up_rate: float | None = Field(None, description="Share of test windows that actually closed higher")
    edge_vs_baseline: float | None = None
    significant: bool = False


class ChartPoint(BaseModel):
    date: dt.date
    open: float
    high: float
    low: float
    close: float
    sma20: float | None = None
    sma50: float | None = None
    sma200: float | None = None
    bb_upper: float | None = None
    bb_lower: float | None = None


class Forecast(BaseModel):
    """Output of core/forecast.py. Computed by code; the LLM only explains it."""

    kind: Literal["statistical_forecast"] = "statistical_forecast"
    as_of: dt.date
    horizon_days: int
    last_close: float
    currency: str
    direction: Literal["up", "down", "no_clear_edge"]
    probability_up: float = Field(ge=0, le=1)
    model_used: str
    consensus_score: float = Field(description="Equal-weight mean of all indicator votes, -1..+1")
    adaptive_score: float | None = None
    confidence: Literal["low", "moderate"]
    confidence_reason: str
    expected_low: float
    expected_high: float
    sigma_pct: float = Field(description="1-sigma move over the horizon, in %")
    indicators: list[IndicatorReading]
    backtest: BacktestReport | None
    chart: list[ChartPoint]
    disclaimer: str


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------


class GeneratedInterpretation(BaseModel):
    """Everything in here was written by a model. Nothing in here is a source fact."""

    kind: Literal["generated_interpretation"] = "generated_interpretation"
    generated_by: str
    headline: str
    claims: list[Claim]
    signals: list[Signal]
    overall_stance: Stance
    confidence: Confidence
    caveats: list[str]


class RemovedItem(BaseModel):
    where: Literal["headline", "claim", "signal", "caveat"]
    text: str
    reasons: list[str]


class ValidationReport(BaseModel):
    passed: bool
    checked_items: int
    repair_attempted: bool = False
    removed: list[RemovedItem] = Field(default_factory=list)
    auto_fixes: list[str] = Field(default_factory=list)
    freshness_notice_included: bool = True


class StageRecord(BaseModel):
    stage: str
    status: Literal["ok", "degraded", "skipped", "failed"] = "ok"
    started_at: AwareDatetime
    duration_ms: float = 0.0
    detail: str = ""


class Metrics(BaseModel):
    latency_ms: float
    tool_calls: int
    tool_call_budget: int
    llm_calls: int


class AnalyzeResponse(BaseModel):
    request_id: str
    symbol: str
    question: str
    status: Literal["ok", "partial", "insufficient_data", "error"]
    answer: GeneratedInterpretation | None = None
    reported_facts: list[Fact] = Field(default_factory=list)
    computed_metrics: list[Fact] = Field(default_factory=list)
    freshness: FreshnessReport | None = None
    forecast: Forecast | None = None
    flags: list[Flag] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    validation: ValidationReport | None = None
    report_markdown: str = ""
    metrics: Metrics
    trace: list[StageRecord] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
