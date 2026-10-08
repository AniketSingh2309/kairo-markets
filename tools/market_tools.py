"""External-data tools with a strict contract.

Each tool:
  1. validates its inputs with a Pydantic model *before* touching the provider
     (bad input -> ``InvalidInputError``, provider is never called);
  2. calls the provider under a hard timeout (-> ``ToolTimeoutError``);
  3. maps provider failures to distinct typed errors
     (``UnknownSymbolError``, ``DataNotFoundError``, ``DataSourceUnavailableError``);
  4. validates the raw payload (-> ``MalformedResponseError`` on any schema,
     consistency, symbol-mismatch or timestamp problem);
  5. returns a ``ToolResponse`` carrying the data, a ``SourceRef`` and
     ``observed_at``.

A single call is a single attempt. Retries, backoff and the per-request call
budget live in ``tools.executor.ToolExecutor``.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from core.clock import Clock, utc_now
from core.config import get_settings
from tools.errors import (
    DataNotFoundError,
    DataSourceUnavailableError,
    InvalidInputError,
    MalformedResponseError,
    ToolError,
    UnknownSymbolError,
)
from tools.models import (
    PERIOD_DAYS,
    Announcements,
    AnnouncementsInput,
    LiveQuote,
    LiveQuoteInput,
    Bars,
    BarsInput,
    Chart,
    ChartInput,
    CompanyInfo,
    CompanyInput,
    Fundamentals,
    FundamentalsInput,
    DailySnapshot,
    DailySnapshotInput,
    Profile,
    ProfileInput,
    PriceHistory,
    PriceHistoryInput,
    PriceQuote,
    SourceRef,
    StockPriceInput,
    ToolResponse,
)
from tools.providers import (
    MarketDataProvider,
    NoDataAvailable,
    ProviderUnavailable,
    SymbolNotFound,
    get_default_provider,
)
from tools.timeouts import call_with_timeout

M = TypeVar("M", bound=BaseModel)

# Allow a little clock skew between the source and us before calling a
# timestamp "in the future".
_FUTURE_TOLERANCE = dt.timedelta(minutes=5)


def describe_validation_error(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(p) for p in err["loc"]) or "input"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


def validate_input(model: type[M], tool: str, **kwargs: Any) -> M:
    try:
        return model(**kwargs)
    except ValidationError as exc:
        raise InvalidInputError(
            f"{tool} rejected its input: {describe_validation_error(exc)}",
            tool=tool,
            symbol=str(kwargs.get("symbol")),
        ) from None


@dataclass(frozen=True)
class _CallContext:
    provider: MarketDataProvider
    timeout_s: float
    clock: Clock


def _context(
    provider: MarketDataProvider | None, timeout_s: float | None, clock: Clock | None
) -> _CallContext:
    return _CallContext(
        provider=provider or get_default_provider(),
        timeout_s=timeout_s if timeout_s is not None else get_settings().tool_timeout_seconds,
        clock=clock or utc_now,
    )


def _call_provider(tool: str, symbol: str, ctx: _CallContext, fn: Callable[[], Any]) -> Any:
    try:
        return call_with_timeout(fn, ctx.timeout_s, tool=tool, symbol=symbol)
    except ToolError:
        raise
    except SymbolNotFound as exc:
        raise UnknownSymbolError(
            f"unknown symbol {symbol!r} ({exc})", tool=tool, symbol=symbol
        ) from exc
    except NoDataAvailable as exc:
        raise DataNotFoundError(str(exc), tool=tool, symbol=symbol) from exc
    except (ProviderUnavailable, ConnectionError, OSError) as exc:
        raise DataSourceUnavailableError(
            f"{ctx.provider.name} unavailable: {exc}", tool=tool, symbol=symbol
        ) from exc


def _build_response(
    tool: str,
    params: BaseModel,
    ctx: _CallContext,
    raw: Any,
    data_model: type[M],
    fetched_at: dt.datetime,
    started: float,
) -> ToolResponse[M]:
    symbol: str = params.symbol  # type: ignore[attr-defined]
    try:
        if not isinstance(raw, Mapping):
            raise TypeError(f"expected a mapping, got {type(raw).__name__}")
        meta = raw["meta"]
        response = ToolResponse[data_model](  # type: ignore[valid-type]
            tool=tool,
            query=params.model_dump(mode="json"),
            data=data_model.model_validate(raw["data"]),
            source=SourceRef(
                provider=ctx.provider.name, source_id=meta["source_id"], url=meta.get("url")
            ),
            observed_at=meta["observed_at"],
            fetched_at=fetched_at,
            latency_ms=(time.perf_counter() - started) * 1000,
        )
    except ValidationError as exc:
        raise MalformedResponseError(
            f"{tool} returned a malformed payload: {describe_validation_error(exc)}",
            tool=tool,
            symbol=symbol,
        ) from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise MalformedResponseError(
            f"{tool} returned a malformed payload: {type(exc).__name__}: {exc}",
            tool=tool,
            symbol=symbol,
        ) from exc

    if response.data.symbol != symbol:  # type: ignore[attr-defined]
        raise MalformedResponseError(
            f"{tool} asked for {symbol} but the payload is for "
            f"{response.data.symbol}",  # type: ignore[attr-defined]
            tool=tool,
            symbol=symbol,
        )
    if response.observed_at > fetched_at + _FUTURE_TOLERANCE:
        raise MalformedResponseError(
            f"{tool} payload claims observed_at {response.observed_at.isoformat()}, "
            "which is in the future",
            tool=tool,
            symbol=symbol,
        )
    return response


# ---------------------------------------------------------------------------
# Public tools
# ---------------------------------------------------------------------------


def get_stock_price(
    symbol: str,
    date: dt.date | str | None = None,
    *,
    provider: MarketDataProvider | None = None,
    timeout_s: float | None = None,
    clock: Clock | None = None,
) -> ToolResponse[PriceQuote]:
    """Daily OHLCV bar for ``symbol`` on ``date`` (latest bar if ``date`` is None)."""
    tool = "get_stock_price"
    params = validate_input(StockPriceInput, tool, symbol=symbol, date=date)
    ctx = _context(provider, timeout_s, clock)
    fetched_at = ctx.clock()
    if params.date is not None and params.date > fetched_at.date():
        raise InvalidInputError(
            f"{tool} rejected its input: date {params.date.isoformat()} is in the future",
            tool=tool,
            symbol=params.symbol,
        )
    started = time.perf_counter()
    raw = _call_provider(
        tool, params.symbol, ctx, lambda: ctx.provider.fetch_quote(params.symbol, params.date)
    )
    response = _build_response(tool, params, ctx, raw, PriceQuote, fetched_at, started)
    if params.date is not None and response.data.bar.date != params.date:
        raise MalformedResponseError(
            f"{tool} asked for {params.date.isoformat()} but received a bar for "
            f"{response.data.bar.date.isoformat()}",
            tool=tool,
            symbol=params.symbol,
        )
    return response


def get_price_history(
    symbol: str,
    period: str = "30d",
    *,
    provider: MarketDataProvider | None = None,
    timeout_s: float | None = None,
    clock: Clock | None = None,
) -> ToolResponse[PriceHistory]:
    """Daily bars for ``symbol`` over ``period`` (7d/30d/90d/180d/1y)."""
    tool = "get_price_history"
    params = validate_input(PriceHistoryInput, tool, symbol=symbol, period=period)
    ctx = _context(provider, timeout_s, clock)
    fetched_at = ctx.clock()
    started = time.perf_counter()
    raw = _call_provider(
        tool,
        params.symbol,
        ctx,
        lambda: ctx.provider.fetch_price_history(params.symbol, PERIOD_DAYS[params.period]),
    )
    return _build_response(tool, params, ctx, raw, PriceHistory, fetched_at, started)


def get_announcements(
    symbol: str,
    period: str = "30d",
    *,
    provider: MarketDataProvider | None = None,
    timeout_s: float | None = None,
    clock: Clock | None = None,
) -> ToolResponse[Announcements]:
    """Company announcements for ``symbol`` over ``period`` (7d/30d/90d/180d/1y)."""
    tool = "get_announcements"
    params = validate_input(AnnouncementsInput, tool, symbol=symbol, period=period)
    ctx = _context(provider, timeout_s, clock)
    fetched_at = ctx.clock()
    started = time.perf_counter()
    raw = _call_provider(
        tool,
        params.symbol,
        ctx,
        lambda: ctx.provider.fetch_announcements(params.symbol, PERIOD_DAYS[params.period]),
    )
    return _build_response(tool, params, ctx, raw, Announcements, fetched_at, started)


def get_live_quote(
    symbol: str,
    *,
    provider: MarketDataProvider | None = None,
    timeout_s: float | None = None,
    clock: Clock | None = None,
) -> ToolResponse[LiveQuote]:
    """Latest regular-session price and today's intraday path (for the live chart)."""
    tool = "get_live_quote"
    params = validate_input(LiveQuoteInput, tool, symbol=symbol)
    ctx = _context(provider, timeout_s, clock)
    fetched_at = ctx.clock()
    started = time.perf_counter()
    fetch = getattr(ctx.provider, "fetch_live", None)
    if fetch is None:
        raise DataNotFoundError(f"{ctx.provider.name} has no live quotes", tool=tool, symbol=params.symbol)
    raw = _call_provider(tool, params.symbol, ctx, lambda: fetch(params.symbol))
    return _build_response(tool, params, ctx, raw, LiveQuote, fetched_at, started)


def get_daily_snapshot(
    symbol: str,
    *,
    provider: MarketDataProvider | None = None,
    timeout_s: float | None = None,
    clock: Clock | None = None,
) -> ToolResponse[DailySnapshot]:
    """Latest session change, volume vs its 20-session average, 52-week range and a month of closes."""
    tool = "get_daily_snapshot"
    params = validate_input(DailySnapshotInput, tool, symbol=symbol)
    ctx = _context(provider, timeout_s, clock)
    fetched_at = ctx.clock()
    started = time.perf_counter()
    fetch = getattr(ctx.provider, "fetch_daily_snapshot", None)
    if fetch is None:
        raise DataNotFoundError(f"{ctx.provider.name} has no daily snapshots", tool=tool, symbol=params.symbol)
    raw = _call_provider(tool, params.symbol, ctx, lambda: fetch(params.symbol))
    return _build_response(tool, params, ctx, raw, DailySnapshot, fetched_at, started)


def get_chart(
    symbol: str,
    chart_range: str = "6M",
    *,
    provider: MarketDataProvider | None = None,
    timeout_s: float | None = None,
    clock: Clock | None = None,
) -> ToolResponse[Chart]:
    """Candles for a chart range (1D, 1W, 1M, 3M, 6M, 1Y, 5Y, ALL), sized to suit it."""
    tool = "get_chart"
    params = validate_input(ChartInput, tool, symbol=symbol, range=chart_range)
    ctx = _context(provider, timeout_s, clock)
    fetched_at = ctx.clock()
    started = time.perf_counter()
    fetch = getattr(ctx.provider, "fetch_chart", None)
    if fetch is None:
        raise DataNotFoundError(f"{ctx.provider.name} has no charts", tool=tool, symbol=params.symbol)
    raw = _call_provider(tool, params.symbol, ctx, lambda: fetch(params.symbol, params.range))
    return _build_response(tool, params, ctx, raw, Chart, fetched_at, started)


def get_bars(
    symbol: str,
    interval: str = "5m",
    *,
    provider: MarketDataProvider | None = None,
    timeout_s: float | None = None,
    clock: Clock | None = None,
) -> ToolResponse[Bars]:
    """Candles at 1m, 5m, 15m, 1h or 1D for the trading terminal."""
    tool = "get_bars"
    params = validate_input(BarsInput, tool, symbol=symbol, interval=interval)
    ctx = _context(provider, timeout_s, clock)
    fetched_at = ctx.clock()
    started = time.perf_counter()
    fetch = getattr(ctx.provider, "fetch_bars", None)
    if fetch is None:
        raise DataNotFoundError(f"{ctx.provider.name} has no intraday bars", tool=tool, symbol=params.symbol)
    raw = _call_provider(tool, params.symbol, ctx, lambda: fetch(params.symbol, params.interval))
    return _build_response(tool, params, ctx, raw, Bars, fetched_at, started)


def get_fundamentals(
    symbol: str,
    statements: bool = True,
    *,
    provider: MarketDataProvider | None = None,
    timeout_s: float | None = None,
    clock: Clock | None = None,
) -> ToolResponse[Fundamentals]:
    """Valuation and profitability ratios, plus annual / quarterly results when ``statements``."""
    tool = "get_fundamentals"
    params = validate_input(FundamentalsInput, tool, symbol=symbol, statements=statements)
    ctx = _context(provider, timeout_s, clock)
    fetched_at = ctx.clock()
    started = time.perf_counter()
    fetch = getattr(ctx.provider, "fetch_fundamentals", None)
    if fetch is None:
        raise DataNotFoundError(f"{ctx.provider.name} has no fundamentals", tool=tool, symbol=params.symbol)
    raw = _call_provider(tool, params.symbol, ctx, lambda: fetch(params.symbol, params.statements))
    return _build_response(tool, params, ctx, raw, Fundamentals, fetched_at, started)


def get_company(
    symbol: str,
    *,
    provider: MarketDataProvider | None = None,
    timeout_s: float | None = None,
    clock: Clock | None = None,
) -> ToolResponse[CompanyInfo]:
    """What the company does: description, website, headcount, headquarters and key people."""
    tool = "get_company"
    params = validate_input(CompanyInput, tool, symbol=symbol)
    ctx = _context(provider, timeout_s, clock)
    fetched_at = ctx.clock()
    started = time.perf_counter()
    fetch = getattr(ctx.provider, "fetch_company", None)
    if fetch is None:
        raise DataNotFoundError(f"{ctx.provider.name} has no company profiles", tool=tool, symbol=params.symbol)
    raw = _call_provider(tool, params.symbol, ctx, lambda: fetch(params.symbol))
    return _build_response(tool, params, ctx, raw, CompanyInfo, fetched_at, started)


def get_profile(
    symbol: str,
    *,
    provider: MarketDataProvider | None = None,
    timeout_s: float | None = None,
    clock: Clock | None = None,
) -> ToolResponse[Profile]:
    """Company name, sector and industry."""
    tool = "get_profile"
    params = validate_input(ProfileInput, tool, symbol=symbol)
    ctx = _context(provider, timeout_s, clock)
    fetched_at = ctx.clock()
    started = time.perf_counter()
    fetch = getattr(ctx.provider, "fetch_profile", None)
    if fetch is None:
        raise DataNotFoundError(f"{ctx.provider.name} has no profiles", tool=tool, symbol=params.symbol)
    raw = _call_provider(tool, params.symbol, ctx, lambda: fetch(params.symbol))
    return _build_response(tool, params, ctx, raw, Profile, fetched_at, started)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    fn: Callable[..., ToolResponse[Any]]
    input_model: type[BaseModel]


TOOL_SPECS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec("get_stock_price", get_stock_price, StockPriceInput),
        ToolSpec("get_price_history", get_price_history, PriceHistoryInput),
        ToolSpec("get_announcements", get_announcements, AnnouncementsInput),
    )
}
