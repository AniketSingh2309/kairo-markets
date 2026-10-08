"""Stock research API: fundamentals and results, scorecard against industry peers, shareholding pattern."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from starlette.concurrency import run_in_threadpool

from api import deps
from api.live import _symbol_or_422, get_live_provider
from api.options import get_nse
from core.config import Settings, get_settings
from core.scorecard import closest_by_size, scorecard
from core.universes import Universes
from tools.errors import ToolError, UnknownSymbolError
from tools.market_tools import get_fundamentals
from tools.models import Fundamentals, ToolResponse
from tools.providers.base import ProviderError, SymbolNotFound
from tools.providers.listings import ListingUnavailable
from tools.providers.nse import NseClient

router = APIRouter(tags=["research"])
FUNDAMENTALS: deps.TTLCache[ToolResponse[Fundamentals]] = deps.TTLCache(6 * 3600)
SCORECARDS: deps.TTLCache[dict] = deps.TTLCache(6 * 3600)
MAX_PEERS = 14

# Yahoo sector -> NSE (Nifty) industries, for stocks outside the Nifty 500 lists
SECTOR_TO_NIFTY = {
    "Basic Materials": ["Chemicals", "Metals & Mining", "Construction Materials", "Forest Materials"],
    "Technology": ["Information Technology"],
    "Financial Services": ["Financial Services"],
    "Healthcare": ["Healthcare"],
    "Consumer Cyclical": ["Automobile and Auto Components", "Consumer Durables", "Consumer Services", "Textiles"],
    "Consumer Defensive": ["Fast Moving Consumer Goods"],
    "Energy": ["Oil Gas & Consumable Fuels"],
    "Industrials": ["Capital Goods", "Construction", "Services"],
    "Utilities": ["Power"],
    "Real Estate": ["Realty"],
    "Communication Services": ["Telecommunication", "Media Entertainment & Publication"],
}


async def fundamentals_of(symbol: str, provider: Any, settings: Settings, statements: bool = True) -> ToolResponse[Fundamentals]:
    async def load() -> ToolResponse[Fundamentals]:
        return await run_in_threadpool(get_fundamentals, symbol, statements, provider=provider, timeout_s=settings.tool_timeout_seconds)
    return await FUNDAMENTALS.get((symbol, statements), load)


def _payload(resp: ToolResponse[Fundamentals]) -> dict[str, Any]:
    return {**resp.data.model_dump(mode="json"), "source": resp.source.provider, "source_url": resp.source.url,
            "observed_at": resp.observed_at.isoformat()}


@router.get("/fundamentals/{symbol}")
async def fundamentals(symbol: str, provider=Depends(get_live_provider), settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """Valuation and profitability ratios, plus the last 4 years and 5 quarters of results."""
    sym = _symbol_or_422(symbol)
    try:
        return _payload(await fundamentals_of(sym, provider, settings))
    except UnknownSymbolError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict()) from None
    except ToolError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict()) from None


def peer_candidates(symbol: str, sector: str | None, market_cap: float | None, registry: Universes) -> tuple[str | None, list[str]]:
    """(group label, up to MAX_PEERS peer symbols nearest in size) from the official lists."""
    store = registry.store
    if store is None:
        return None, []
    try:
        if symbol.endswith((".NS", ".BO")):
            nse_sym = registry.venues(symbol).get("NSE") or symbol
            n500 = store.nifty("nifty500")
            own = next((m.sector for m in n500 if m.symbol == nse_sym), None)
            industries = [own] if own else SECTOR_TO_NIFTY.get(sector or "", [])
            if not industries:
                return None, []
            caps = {m.isin: m.market_cap for m in store.bse_equities() if m.isin}
            isin = {m.symbol: m.isin for m in store.nse_equities()}
            pool = [(m.symbol, caps.get(isin.get(m.symbol))) for m in n500 if m.sector in industries and m.symbol != nse_sym]
            label = industries[0] if own else " / ".join(industries)
            return f"{label} (NSE)", closest_by_size(pool, market_cap, MAX_PEERS)
        if "." not in symbol and not symbol.startswith("^"):
            us = store.us_stocks()
            own = next((m for m in us if m.symbol == symbol), None)
            sec = own.sector if own else None
            if not sec:
                return None, []
            seen = {own.name.lower()} if own else set()
            pool = []
            for m in sorted((m for m in us if m.sector == sec and m.symbol != symbol and m.market_cap), key=lambda m: -m.market_cap):
                if m.name.lower() not in seen:  # one line per company (GOOG / GOOGL)
                    seen.add(m.name.lower()); pool.append((m.symbol, m.market_cap))
            return f"{sec} (US)", closest_by_size(pool, market_cap or (own.market_cap if own else None), MAX_PEERS)
    except (ListingUnavailable, ValueError):
        return None, []
    return None, []


@router.get("/fundamentals/{symbol}/scorecard")
async def stock_scorecard(symbol: str, provider=Depends(get_live_provider), settings: Settings = Depends(get_settings),
                          registry: Universes = Depends(deps.get_universes)) -> dict[str, Any]:
    """Five 0-100 scores against industry peers, the peers' key ratios, and industry medians."""
    sym = _symbol_or_422(symbol)

    async def build() -> dict:
        try:
            me = (await fundamentals_of(sym, provider, settings, statements=False)).data
        except UnknownSymbolError as exc:
            raise HTTPException(status_code=404, detail=exc.to_dict()) from None
        except ToolError as exc:
            raise HTTPException(status_code=503, detail=exc.to_dict()) from None
        label, peers = await run_in_threadpool(peer_candidates, sym, me.sector, me.market_cap, registry)
        results = await deps.gather_limited(peers, lambda s: fundamentals_of(s, provider, settings, statements=False), limit=6)
        rows = [r.data.model_dump(mode="json") for r in results if not isinstance(r, BaseException)]
        card = scorecard(me.model_dump(mode="json"), rows)
        keep = ("symbol", "name", "market_cap", "pe", "pb", "roe", "net_margin", "revenue_growth", "year_change", "debt_to_equity", "currency")
        return {"symbol": sym, "group": label, **card,
                "peers": sorted(({k: r.get(k) for k in keep} for r in rows), key=lambda r: -(r["market_cap"] or 0)),
                "me": {k: me.model_dump(mode="json").get(k) for k in keep}}
    return await SCORECARDS.get(sym, build)


@router.get("/shareholding/{symbol}")
async def shareholding(symbol: str, nse: NseClient = Depends(get_nse), registry: Universes = Depends(deps.get_universes)) -> dict[str, Any]:
    """Promoter / FII / mutual fund / other DII / retail holdings for the last 4 quarters (NSE filings)."""
    sym = _symbol_or_422(symbol)
    if not sym.endswith((".NS", ".BO")):
        raise HTTPException(status_code=404, detail="shareholding patterns are available for NSE-listed companies")
    nse_sym = (await run_in_threadpool(registry.venues, sym)).get("NSE") or (sym if sym.endswith(".NS") else None)
    if not nse_sym:
        raise HTTPException(status_code=404, detail="this company isn't listed on NSE")
    try:
        data = await run_in_threadpool(nse.shareholding, nse_sym.removesuffix(".NS"))
    except SymbolNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return {**data, "symbol": sym, "source": "NSE shareholding pattern filings"}

