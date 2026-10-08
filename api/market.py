"""Fast, LLM-free market endpoints used by the app shell.

GET /forecast/{symbol}?horizon_days=5 -> the statistical outlook, backtest and 6-month chart.
Runs in ~1 s because it skips the agents; the full cited analysis stays on POST /analyze.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from api import deps

from api.live import _symbol_or_422, get_live_provider
from core.config import Settings, get_settings
from core import technicals as ta
from core.forecast import MIN_BARS, build_forecast
from core.universes import Universes
from tools.errors import InvalidInputError, ToolError, UnknownSymbolError
from tools.market_tools import get_bars, get_chart, get_company, get_price_history
from tools.models import Bars, Chart, CompanyInfo, ToolResponse, is_mf_symbol
from tools.providers import MarketDataProvider

router = APIRouter(tags=["market"])
CHARTS_INTRADAY: deps.TTLCache[ToolResponse[Chart]] = deps.TTLCache(30)
CHARTS_DAILY: deps.TTLCache[ToolResponse[Chart]] = deps.TTLCache(10 * 60)
COMPANIES: deps.TTLCache[ToolResponse[CompanyInfo]] = deps.TTLCache(24 * 3600)
BAR_CACHES: dict[str, deps.TTLCache[ToolResponse[Bars]]] = {
    "1m": deps.TTLCache(20), "5m": deps.TTLCache(60), "15m": deps.TTLCache(120), "1h": deps.TTLCache(300), "1D": deps.TTLCache(600),
}


@router.get("/terminal/bars/{symbol}")
async def terminal_bars(
    symbol: str,
    interval: str = Query("5m", pattern="^(1m|5m|15m|1h|1D)$"),
    provider: MarketDataProvider = Depends(get_live_provider),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Terminal chart data, column-wise (t in epoch seconds) so thousands of candles stay small."""
    sym = _symbol_or_422(symbol)

    async def load() -> ToolResponse[Bars]:
        return await run_in_threadpool(get_bars, sym, interval, provider=provider, timeout_s=settings.tool_timeout_seconds)
    try:
        resp = await BAR_CACHES[interval].get(sym, load)
    except UnknownSymbolError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict()) from None
    except InvalidInputError as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from None
    except ToolError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict()) from None
    d = resp.data
    return {"symbol": d.symbol, "name": d.name, "currency": d.currency, "interval": d.interval,
            "previous_close": d.previous_close, "observed_at": resp.observed_at.isoformat(),
            "t": [int(b.t.timestamp()) for b in d.bars], "o": [b.open for b in d.bars], "h": [b.high for b in d.bars],
            "l": [b.low for b in d.bars], "c": [b.close for b in d.bars], "v": [b.volume or 0 for b in d.bars]}


@router.get("/company/{symbol}")
async def company(symbol: str, provider: MarketDataProvider = Depends(get_live_provider),
                  settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """About the company: the provider's business description, key facts and people (cached for a day)."""
    sym = _symbol_or_422(symbol)

    async def load() -> ToolResponse[CompanyInfo]:
        return await run_in_threadpool(get_company, sym, provider=provider, timeout_s=settings.tool_timeout_seconds)
    try:
        resp = await COMPANIES.get(sym, load)
    except UnknownSymbolError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict()) from None
    except InvalidInputError as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from None
    except ToolError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict()) from None
    return {**resp.data.model_dump(mode="json"), "source": resp.source.provider, "source_url": resp.source.url,
            "observed_at": resp.observed_at.isoformat()}


def _chart_payload(resp: ToolResponse[Chart]) -> dict[str, Any]:
    """Visible bars with moving averages and Bollinger bands computed over the warm-up bars too."""
    c = resp.data
    closes = [b.close for b in c.bars]
    sma20, sma50, sma200 = ta.sma(closes, 20), ta.sma(closes, 50), ta.sma(closes, 200)
    _, bb_up, bb_lo = ta.bollinger(closes, 20, 2.0)
    r = lambda v: None if v is None else round(v, 4)  # noqa: E731
    bars = [{"t": b.t.isoformat(), "o": b.open, "h": b.high, "l": b.low, "c": b.close, "v": b.volume,
             "sma20": r(sma20[i]), "sma50": r(sma50[i]), "sma200": r(sma200[i]), "bb_upper": r(bb_up[i]), "bb_lower": r(bb_lo[i])}
            for i, b in enumerate(c.bars) if b.t >= c.visible_from]
    if not bars:
        bars = [{"t": c.bars[-1].t.isoformat(), "o": c.bars[-1].open, "h": c.bars[-1].high, "l": c.bars[-1].low,
                 "c": c.bars[-1].close, "v": c.bars[-1].volume}]
    base = c.previous_close or bars[0]["o"]
    last = bars[-1]["c"]
    return {"symbol": c.symbol, "name": c.name, "currency": c.currency, "range": c.range, "interval": c.interval,
            "intraday": c.intraday, "previous_close": c.previous_close,
            "change": round(last - base, 6), "change_pct": round((last / base - 1) * 100, 4) if base else None,
            "high": max(b["h"] for b in bars), "low": min(b["l"] for b in bars), "bars": bars,
            "source_id": resp.source.source_id, "observed_at": resp.observed_at.isoformat()}


@router.get("/chart/{symbol}")
async def chart(
    symbol: str,
    range: str = Query("6M", pattern="^(1D|1W|1M|3M|6M|1Y|5Y|ALL)$"),  # noqa: A002 - the API's own word
    provider: MarketDataProvider = Depends(get_live_provider),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Candles for 1D (5-minute) through ALL (monthly), with SMA 20/50/200 and Bollinger bands."""
    sym = _symbol_or_422(symbol)
    cache = CHARTS_INTRADAY if range in ("1D", "1W", "1M") else CHARTS_DAILY

    async def load() -> ToolResponse[Chart]:
        return await run_in_threadpool(get_chart, sym, range, provider=provider, timeout_s=settings.tool_timeout_seconds)
    stale = False
    try:
        resp = await cache.get((sym, range), load)
        _remember_chart((sym, range), resp)
    except UnknownSymbolError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict()) from None
    except InvalidInputError as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from None
    except ToolError as exc:
        # A failed refresh serves the last good chart (marked stale) rather than an error.
        if (resp := _LAST_GOOD.get((sym, range))) is None:
            raise HTTPException(status_code=503, detail=exc.to_dict()) from None
        stale = True
    return {**await run_in_threadpool(_chart_payload, resp), "stale": stale}


_LAST_GOOD: dict[tuple[str, str], ToolResponse[Chart]] = {}


def _remember_chart(key: tuple[str, str], resp: ToolResponse[Chart]) -> None:
    _LAST_GOOD.pop(key, None)
    _LAST_GOOD[key] = resp
    while len(_LAST_GOOD) > 400:
        _LAST_GOOD.pop(next(iter(_LAST_GOOD)))


@router.get("/logo/{symbol}")
async def logo(symbol: str = Path(..., max_length=24), registry: Universes = Depends(deps.get_universes),
               logos=Depends(deps.get_logo_store)) -> Response:
    """The company's logo (cached on disk), or 404 so the app shows its initials instead."""
    try:
        sym = _symbol_or_422(symbol)
    except HTTPException:
        raise HTTPException(status_code=404, detail="no logo") from None
    if is_mf_symbol(sym):
        raise HTTPException(status_code=404, detail="no logo")
    listing = await run_in_threadpool(registry.listing, sym)
    found = await logos.get(sym, listing.isin if listing else None)
    if found is None:
        raise HTTPException(status_code=404, detail="no logo", headers={"Cache-Control": "public, max-age=86400"})
    return Response(found.content, media_type=found.content_type,
                    headers={"Cache-Control": "public, max-age=604800", "X-Content-Type-Options": "nosniff"})


@router.get("/forecast/{symbol}")
async def forecast(
    symbol: str,
    horizon_days: int = Query(5, ge=1, le=20),
    provider: MarketDataProvider = Depends(get_live_provider),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    sym = _symbol_or_422(symbol)
    try:
        history = await run_in_threadpool(get_price_history, sym, "2y", provider=provider,
                                          timeout_s=settings.tool_timeout_seconds)
    except UnknownSymbolError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict()) from None
    except InvalidInputError as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from None
    except ToolError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict()) from None

    bars = history.data.bars
    fc = await run_in_threadpool(build_forecast, bars, horizon_days, history.data.currency)
    return {
        "symbol": sym,
        "source_id": history.source.source_id,
        "observed_at": history.observed_at.isoformat(),
        "forecast": fc.model_dump(mode="json") if fc else None,
        "reason": None if fc else f"needs at least {MIN_BARS} daily bars; {len(bars)} available",
    }
