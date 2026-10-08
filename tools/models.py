"""Pydantic models for tool inputs, tool outputs and the response envelope."""

from __future__ import annotations

import datetime as dt
import re
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# ---------------------------------------------------------------------------
# Input primitives
# ---------------------------------------------------------------------------

# A 1-10 character base of letters, digits and "&" containing at least one letter (NSE lists
# 20MICRONS, M&M, J&KBANK), then an optional -class/-quote part (BRK-B, BAJAJ-AUTO, BTC-USD) and an
# optional .exchange / .class suffix (INFY.NS, BRK.B).
# Optional ^ for indices (^NSEI), optional =X / =F for FX and futures (USDINR=X, GC=F).
SYMBOL_PATTERN = re.compile(r"^\^?(?=[A-Z0-9&]*[A-Z])[A-Z0-9][A-Z0-9&]{0,9}(?:-[A-Z0-9]{1,6})?(?:\.[A-Z0-9]{1,4})?(?:=[XF])?$")

Period = Literal["7d", "30d", "90d", "180d", "1y", "2y", "5y"]
PERIOD_DAYS: dict[str, int] = {
    "7d": 7, "30d": 30, "90d": 90, "180d": 180, "1y": 365, "2y": 730, "5y": 1825,
}


def normalize_symbol(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("symbol must be a string")
    symbol = value.strip().upper()
    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise ValueError(f"invalid symbol format: {value!r}")
    return symbol


Symbol = Annotated[str, BeforeValidator(normalize_symbol)]

_MF_RE = re.compile(r"^MF\d{3,8}$")


def is_mf_symbol(symbol: str) -> bool:
    """Mutual funds are addressed as MF<AMFI scheme code>, e.g. MF122639."""
    return bool(_MF_RE.fullmatch(symbol or ""))


def mf_code(symbol: str) -> str:
    return symbol[2:]


def mf_symbol(code: str | int) -> str:
    return f"MF{code}"


class _ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StockPriceInput(_ToolInput):
    symbol: Symbol
    date: dt.date | None = Field(None, description="Trading day (YYYY-MM-DD); omit for latest.")

    @field_validator("date")
    @classmethod
    def _not_ancient(cls, value: dt.date | None) -> dt.date | None:
        if value is not None and value.year < 1970:
            raise ValueError("date must be on or after 1970-01-01")
        return value


class PriceHistoryInput(_ToolInput):
    symbol: Symbol
    period: Period = "30d"


class AnnouncementsInput(_ToolInput):
    symbol: Symbol
    period: Period = "30d"


# ---------------------------------------------------------------------------
# Data payloads
# ---------------------------------------------------------------------------


class PriceBar(BaseModel):
    model_config = ConfigDict(frozen=True)

    date: dt.date
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: int = Field(ge=0)

    @model_validator(mode="after")
    def _ohlc_consistent(self) -> PriceBar:
        if self.high < self.low:
            raise ValueError(f"high {self.high} < low {self.low} on {self.date}")
        for name in ("open", "close"):
            value = getattr(self, name)
            if not self.low <= value <= self.high:
                raise ValueError(f"{name} {value} outside [low, high] on {self.date}")
        return self


class PriceQuote(BaseModel):
    symbol: str
    currency: str = Field(min_length=3, max_length=3)
    bar: PriceBar


class PriceHistory(BaseModel):
    symbol: str
    name: str | None = None
    currency: str = Field(min_length=3, max_length=3)
    bars: list[PriceBar] = Field(min_length=1)

    @field_validator("bars")
    @classmethod
    def _sorted_unique(cls, bars: list[PriceBar]) -> list[PriceBar]:
        ordered = sorted(bars, key=lambda b: b.date)
        dates = [b.date for b in ordered]
        if len(set(dates)) != len(dates):
            raise ValueError("duplicate trading dates in price history")
        return ordered


class Announcement(BaseModel):
    id: str = Field(min_length=1)
    published_at: AwareDatetime
    category: str = Field(min_length=1)
    title: str = Field(min_length=1)
    summary: str = ""
    url: str | None = None
    image_url: str | None = Field(None, description="Full-size picture the publisher attached, if any")
    thumbnail_url: str | None = Field(None, description="Small square version of the picture, if any")

    @field_validator("image_url", "thumbnail_url")
    @classmethod
    def _https_only(cls, v: str | None) -> str | None:
        return v if v and v.startswith("https://") else None


class Announcements(BaseModel):
    symbol: str
    items: list[Announcement]


class DailySnapshotInput(_ToolInput):
    symbol: Symbol


ChartRange = Literal["1D", "1W", "1M", "3M", "6M", "1Y", "5Y", "ALL"]


class ChartInput(_ToolInput):
    symbol: Symbol
    range: ChartRange = "6M"


class ChartBar(BaseModel):
    """One candle. ``t`` is the bar's start in UTC (a session date at 00:00 for daily and longer bars)."""

    model_config = ConfigDict(frozen=True)

    t: AwareDatetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: int | None = Field(None, ge=0)

    @model_validator(mode="after")
    def _ohlc_consistent(self) -> ChartBar:
        if self.high < self.low:
            raise ValueError(f"high {self.high} < low {self.low} at {self.t}")
        return self


BarInterval = Literal["1m", "5m", "15m", "1h", "1D"]


class BarsInput(_ToolInput):
    symbol: Symbol
    interval: BarInterval = "5m"


class Bars(BaseModel):
    """Candles at one interval for the trading terminal (as much history as the source keeps)."""

    symbol: str
    name: str | None = None
    currency: str = Field(min_length=3, max_length=3)
    interval: BarInterval
    previous_close: float | None = Field(None, gt=0, description="Previous session's close")
    bars: list[ChartBar] = Field(min_length=1)

    @field_validator("bars")
    @classmethod
    def _sorted(cls, bars: list[ChartBar]) -> list[ChartBar]:
        return sorted({b.t: b for b in bars}.values(), key=lambda b: b.t)


class Chart(BaseModel):
    """Candles for one chart range, plus earlier bars (``visible_from``) so moving averages are warm."""

    symbol: str
    name: str | None = None
    currency: str = Field(min_length=3, max_length=3)
    range: ChartRange
    interval: str = Field(description="Bar size: 5m, 15m, 1h, 1d, 1wk or 1mo")
    intraday: bool
    previous_close: float | None = Field(None, gt=0, description="Close before the range starts (1D: yesterday)")
    visible_from: AwareDatetime
    bars: list[ChartBar] = Field(min_length=1)

    @field_validator("bars")
    @classmethod
    def _sorted(cls, bars: list[ChartBar]) -> list[ChartBar]:
        return sorted({b.t: b for b in bars}.values(), key=lambda b: b.t)


class DailySnapshot(BaseModel):
    """Latest session vs the previous one, with a month of daily closes (for movers & sparklines)."""

    symbol: str
    name: str | None = None
    exchange: str | None = None
    currency: str = Field(min_length=3, max_length=3)
    price: float = Field(gt=0)
    previous_close: float | None = Field(None, gt=0)
    change: float | None = None
    change_pct: float | None = None
    volume: int | None = Field(None, ge=0)
    avg_volume: float | None = Field(None, ge=0, description="Mean volume of the prior 20 sessions")
    volume_ratio: float | None = None
    fifty_two_week_high: float | None = None
    fifty_two_week_low: float | None = None
    session_date: dt.date
    market_time: AwareDatetime
    closes: list[float] = Field(min_length=1)

    @model_validator(mode="after")
    def _derive(self) -> DailySnapshot:
        if self.previous_close:
            self.change = round(self.price - self.previous_close, 6)
            self.change_pct = round(self.change / self.previous_close * 100, 4)
        if self.volume is not None and self.avg_volume:
            self.volume_ratio = round(self.volume / self.avg_volume, 3)
        return self


class ProfileInput(_ToolInput):
    symbol: Symbol


class Profile(BaseModel):
    symbol: str
    name: str | None = None
    sector: str | None = None
    industry: str | None = None
    quote_type: str | None = None
    exchange: str | None = None


class FundamentalsInput(_ToolInput):
    symbol: Symbol
    statements: bool = True


class FinancialPeriod(BaseModel):
    """One reporting period (fiscal year or quarter), in the stock's currency."""

    period_end: dt.date
    revenue: float | None = None
    operating_income: float | None = None
    net_income: float | None = None
    eps: float | None = None
    equity: float | None = None
    debt: float | None = None
    free_cash_flow: float | None = None


class Fundamentals(BaseModel):
    """Valuation, profitability, balance-sheet and growth figures, plus reported results.

    Ratios are plain fractions (0.15 = 15%), except ``debt_to_equity`` which is a multiple (0.46x).
    """

    symbol: str
    name: str | None = None
    currency: str | None = None
    sector: str | None = None
    industry: str | None = None
    market_cap: float | None = None
    pe: float | None = None
    forward_pe: float | None = None
    pb: float | None = None
    ps: float | None = None
    ev_ebitda: float | None = None
    eps: float | None = None
    book_value: float | None = None
    dividend_yield: float | None = None
    payout_ratio: float | None = None
    roe: float | None = None
    net_margin: float | None = None
    operating_margin: float | None = None
    gross_margin: float | None = None
    debt_to_equity: float | None = None
    revenue_growth: float | None = None
    earnings_growth: float | None = None
    beta: float | None = None
    year_change: float | None = None
    annual: list[FinancialPeriod] = Field(default_factory=list)
    quarterly: list[FinancialPeriod] = Field(default_factory=list)


class CompanyInput(_ToolInput):
    symbol: Symbol


class Officer(BaseModel):
    name: str = Field(min_length=1)
    title: str | None = None
    year_born: int | None = None


class CompanyInfo(BaseModel):
    """What the company does, as published by the data provider (not generated)."""

    symbol: str
    name: str | None = None
    description: str | None = None
    website: str | None = None
    sector: str | None = None
    industry: str | None = None
    employees: int | None = Field(None, ge=0)
    city: str | None = None
    state: str | None = None
    country: str | None = None
    market_cap: float | None = Field(None, ge=0)
    currency: str | None = None
    quote_type: str | None = None
    officers: list[Officer] = Field(default_factory=list)

    @field_validator("website")
    @classmethod
    def _web_only(cls, v: str | None) -> str | None:
        return v if v and v.startswith(("https://", "http://")) else None


class LiveQuoteInput(_ToolInput):
    symbol: Symbol


class IntradayPoint(BaseModel):
    t: AwareDatetime
    price: float = Field(gt=0)


class LiveQuote(BaseModel):
    """Intraday snapshot: latest regular-session price plus today's 1-minute path."""

    symbol: str
    name: str | None = None
    exchange: str | None = None
    exchange_timezone: str | None = None
    currency: str = Field(min_length=3, max_length=3)
    price: float = Field(gt=0)
    market_time: AwareDatetime
    open: float | None = None
    volume: int | None = None
    fifty_two_week_high: float | None = None
    fifty_two_week_low: float | None = None
    previous_close: float | None = Field(None, gt=0)
    change: float | None = None
    change_pct: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    market_state: Literal["open", "closed"]
    session_start: AwareDatetime | None = None
    session_end: AwareDatetime | None = None
    points: list[IntradayPoint] = Field(default_factory=list)

    @model_validator(mode="after")
    def _derive_change(self) -> LiveQuote:
        if self.previous_close:
            self.change = round(self.price - self.previous_close, 6)
            self.change_pct = round(self.change / self.previous_close * 100, 4)
        return self


# ---------------------------------------------------------------------------
# Response envelope
# ---------------------------------------------------------------------------

T = TypeVar("T", bound=BaseModel)


class SourceRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    source_id: str = Field(min_length=1, description="URL or mock source identifier")
    url: str | None = None


class ToolResponse(BaseModel, Generic[T]):
    """What every external-data tool returns.

    ``observed_at`` is when the *data* was true at the source (e.g. market close
    of the bar, or the as-of time of the announcements feed). ``fetched_at`` is
    when we called the tool. Freshness is always judged on ``observed_at``.
    """

    tool: str
    query: dict[str, Any] = Field(description="The validated input the tool was called with")
    data: T
    source: SourceRef
    observed_at: AwareDatetime
    fetched_at: AwareDatetime
    latency_ms: float = Field(ge=0)
