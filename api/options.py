"""Option chain API (NSE): underlyings, chains with Greeks and OI build-up, PCR through the day,
option-contract price series and straddle charts."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from api import deps
from api.live import get_live_provider
from core import options_math as om
from core import strategy as st
from core.store import Store
from tools.errors import ToolError
from tools.models import normalize_symbol
from tools.providers.base import ProviderError, SymbolNotFound
from tools.providers.nse import CONTRACT_RE, EXPIRY_RE, IST, NSE_TO_YAHOO, SYMBOL_RE, YAHOO_TO_NSE, NseClient, contract_label

router = APIRouter(prefix="/options", tags=["options"])
log = logging.getLogger("kairo.options")
SNAPSHOT_EVERY_S = 5 * 60
ALWAYS_RECORD = ("NIFTY", "BANKNIFTY")


@lru_cache(maxsize=1)
def get_nse() -> NseClient:
    from core.config import get_settings

    return NseClient(cache_dir=get_settings().db_path.parent / "nse")


def market_open(now: dt.datetime | None = None) -> bool:
    now = (now or dt.datetime.now(IST)).astimezone(IST)
    return now.weekday() < 5 and dt.time(9, 0) <= now.time() <= dt.time(15, 45)


def yahoo_symbol(nse_symbol: str) -> str:
    return NSE_TO_YAHOO.get(nse_symbol, f"{nse_symbol}.NS")


async def _call(fn, *args, **kwargs):
    try:
        return await run_in_threadpool(fn, *args, **kwargs)
    except SymbolNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@router.get("/underlyings")
async def underlyings(nse: NseClient = Depends(get_nse)) -> dict[str, Any]:
    """Everything with options on NSE: the indices (NIFTY, BANKNIFTY, ...) and the F&O stocks."""
    u = await _call(nse.underlyings)
    add = lambda xs: [{**x, "yahoo": yahoo_symbol(x["symbol"])} for x in xs]  # noqa: E731
    return {"indices": add(u["indices"]), "stocks": add(u["stocks"])}


@router.get("/for/{symbol}")
async def options_for(symbol: str, nse: NseClient = Depends(get_nse)) -> dict[str, Any]:
    """The NSE options underlying for an app symbol (^NSEI -> NIFTY, RELIANCE.NS -> RELIANCE), if it has options."""
    try:
        sym = normalize_symbol(symbol)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"invalid symbol {symbol!r}") from None
    u = await _call(nse.underlyings)
    nse_sym = YAHOO_TO_NSE.get(sym) or (sym.removesuffix(".NS") if sym.endswith(".NS") else None)
    if nse_sym and any(x["symbol"] == nse_sym for x in [*u["indices"], *u["stocks"]]):
        return {"symbol": nse_sym, "yahoo": sym}
    raise HTTPException(status_code=404, detail=f"{sym} has no options on NSE")


def session_open(now: dt.datetime | None = None) -> bool:
    """Regular trading session (09:15-15:30 IST, weekdays): when chain snapshots are worth recording."""
    t = (now or dt.datetime.now(IST)).astimezone(IST)
    return t.weekday() < 5 and dt.time(9, 15) <= t.time() <= dt.time(15, 30)


def enrich(data: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    """The chain with Greeks and OI build-up per side (new objects: the cached chain is never modified)."""
    spot, T = data.get("spot"), om.years_to_expiry(dt.datetime.strptime(data["expiry"], "%d-%b-%Y").date(), now)
    rows = []
    for r in data["rows"]:
        row = {"strike": r["strike"]}
        for key, kind in (("ce", "CE"), ("pe", "PE")):
            side = r.get(key)
            row[key] = None if side is None else {**side, "greeks": om.side_greeks(kind, side, spot, r["strike"], T),
                                                   "buildup": om.buildup(side.get("chg"), side.get("coi"))}
        rows.append(row)
    return {**data, "rows": rows, "days_to_expiry": round(T * 365, 2), "risk_free_rate": om.DEFAULT_RATE}


class OiRecorder:
    """Records PCR, max pain and total OI every few minutes in market hours, for PCR-through-the-day charts.

    NIFTY and BANKNIFTY (nearest expiry) are always recorded while Kairo runs; any other chain is
    recorded while someone has looked at it in the last hour.
    """

    def __init__(self) -> None:
        self.last: dict[tuple[str, str], float] = {}
        self.watched: dict[tuple[str, str | None], float] = {}
        self.task: asyncio.Task | None = None

    def note(self, store: Store, data: dict[str, Any]) -> None:
        key = (data["symbol"], data["expiry"])
        self.watched[key] = asyncio.get_running_loop().time()
        if session_open() and asyncio.get_running_loop().time() - self.last.get(key, -1e9) >= SNAPSHOT_EVERY_S - 30:
            self.last[key] = asyncio.get_running_loop().time()
            t = data["totals"]
            store.add_oi_snapshot({"symbol": data["symbol"], "expiry": data["expiry"], "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                                   "spot": data.get("spot"), "ce_oi": t["ce_oi"], "pe_oi": t["pe_oi"], "pcr": t["pcr"], "max_pain": t["max_pain"]})

    async def run(self, nse: NseClient, store: Store) -> None:
        while True:
            try:
                if session_open():
                    now = asyncio.get_running_loop().time()
                    keys = {(s, None) for s in ALWAYS_RECORD} | {k for k, seen in self.watched.items() if now - seen < 3600}
                    for sym, exp in keys:
                        data = await run_in_threadpool(nse.chain, sym, exp, 60)
                        self.note(store, data)
            except Exception:  # noqa: BLE001 - keep recording
                log.exception("OI snapshot failed")
            await asyncio.sleep(60)

    def ensure_running(self, nse: NseClient, store: Store) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.get_running_loop().create_task(self.run(nse, store))


RECORDER = OiRecorder()


async def vix(provider: Any) -> dict[str, Any] | None:
    try:
        q = await deps.quote("^INDIAVIX", provider)
    except ToolError:
        return None
    chg = (q.price / q.previous_close - 1) * 100 if q.previous_close else None
    return {"value": q.price, "change_pct": round(chg, 2) if chg is not None else None}


def _check(symbol: str, expiry: str | None) -> str:
    symbol = symbol.upper()
    if not SYMBOL_RE.match(symbol) or (expiry and not EXPIRY_RE.match(expiry)):
        raise HTTPException(status_code=422, detail="bad symbol or expiry (expected e.g. NIFTY and 13-Oct-2026)")
    return symbol


@router.get("/chain")
async def chain(symbol: str = Query(..., max_length=20), expiry: str | None = Query(None, max_length=11),
                nse: NseClient = Depends(get_nse), provider=Depends(get_live_provider),
                store: Store = Depends(deps.get_store)) -> dict[str, Any]:
    """One expiry's chain: per strike, calls and puts (LTP, change, OI, change in OI, volume, IV, bid/ask),
    with Greeks and OI build-up, plus India VIX."""
    symbol = _check(symbol, expiry)
    data = await _call(nse.chain, symbol, expiry, ttl=5 if market_open() else 60)
    RECORDER.ensure_running(nse, store)
    RECORDER.note(store, data)
    out = enrich(data, dt.datetime.now(dt.timezone.utc))
    lot = await run_in_threadpool(nse.lot_size, symbol, out["expiry"])
    return {**out, "lot_size": lot, "vix": await vix(provider), "yahoo": yahoo_symbol(symbol), "source": "nseindia.com option chain",
            "market_open": market_open()}


@router.get("/oi-history")
async def oi_history(symbol: str = Query(..., max_length=20), expiry: str = Query(..., max_length=11),
                     store: Store = Depends(deps.get_store)) -> dict[str, Any]:
    """Today's recorded PCR, max pain, total OI and spot (every 5 minutes while Kairo runs in market hours)."""
    symbol = _check(symbol, expiry)
    today = dt.datetime.combine(dt.datetime.now(IST).date(), dt.time(0, 0), tzinfo=IST).astimezone(dt.timezone.utc)
    points = await run_in_threadpool(store.oi_snapshots, symbol, expiry, today.isoformat())
    return {"symbol": symbol, "expiry": expiry, "points": points, "every_minutes": SNAPSHOT_EVERY_S // 60}


def merge_legs(ce: list[tuple[int, float]], pe: list[tuple[int, float]], step: int = 60) -> list[tuple[int, float, float]]:
    """Two trade series -> one per-minute series of (time, call, put), carrying each leg's last price forward."""
    def by_minute(ticks):
        out: dict[int, float] = {}
        for t, p in ticks:
            out[t - t % step] = p
        return out
    a, b = by_minute(ce), by_minute(pe)
    rows, last_a, last_b = [], None, None
    for t in sorted(set(a) | set(b)):
        last_a, last_b = a.get(t, last_a), b.get(t, last_b)
        if last_a is not None and last_b is not None:
            rows.append((t, last_a, last_b))
    return rows


@router.get("/straddle")
async def straddle(symbol: str = Query(..., max_length=20), expiry: str | None = Query(None, max_length=11),
                   strike: float | None = Query(None, gt=0), nse: NseClient = Depends(get_nse)) -> dict[str, Any]:
    """Call + put premium at one strike through today (defaults to the at-the-money strike)."""
    symbol = _check(symbol, expiry)
    data = await _call(nse.chain, symbol, expiry, ttl=5 if market_open() else 60)
    k = strike if strike is not None else data["atm"]
    row = next((r for r in data["rows"] if r["strike"] == k), None)
    if row is None or not (row.get("ce") or {}).get("id") or not (row.get("pe") or {}).get("id"):
        raise HTTPException(status_code=404, detail=f"no call and put at strike {k:g}")
    ttl = 5 if market_open() else 300
    ce, pe = await asyncio.gather(_call(nse.contract, row["ce"]["id"], ttl), _call(nse.contract, row["pe"]["id"], ttl))
    rows = merge_legs(ce["ticks"], pe["ticks"])
    if not rows:
        raise HTTPException(status_code=404, detail="no trades today in one of the legs")
    last = rows[-1][1] + rows[-1][2]
    prev = (ce.get("prev_close") or 0) + (pe.get("prev_close") or 0)
    spot = data.get("spot")
    return {"symbol": symbol, "expiry": data["expiry"], "strike": k, "atm": data["atm"], "spot": spot,
            "strikes": [r["strike"] for r in data["rows"]],
            "t": [r[0] for r in rows], "ce": [r[1] for r in rows], "pe": [r[2] for r in rows], "straddle": [round(r[1] + r[2], 2) for r in rows],
            "premium": round(last, 2), "open": round(rows[0][1] + rows[0][2], 2), "prev_close": round(prev, 2) if prev else None,
            "implied_move_pct": round(last / spot * 100, 2) if spot else None,
            "breakevens": [round(k - last, 2), round(k + last, 2)], "market_open": market_open()}


@router.get("/contract")
async def contract(id: str = Query(..., max_length=60), nse: NseClient = Depends(get_nse)) -> dict[str, Any]:  # noqa: A002
    """Today's trades for one option contract, for the terminal chart."""
    if not CONTRACT_RE.match(id):
        raise HTTPException(status_code=422, detail="not an NSE option contract id")
    d = await _call(nse.contract, id, ttl=5 if market_open() else 300)
    return {"id": d["id"], "label": d["label"], "prev_close": d["prev_close"],
            "t": [t for t, _ in d["ticks"]], "p": [p for _, p in d["ticks"]], "market_open": market_open()}


class LegIn(BaseModel):
    kind: str = Field(pattern="^(CE|PE)$")
    side: str = Field(pattern="^(BUY|SELL)$")
    strike: float = Field(gt=0)
    lots: int = Field(1, ge=1, le=500)


class StrategyIn(BaseModel):
    symbol: str = Field(max_length=20)
    expiry: str | None = Field(None, max_length=11)
    legs: list[LegIn] = Field(min_length=1, max_length=8)


@router.post("/strategy")
async def strategy(body: StrategyIn, nse: NseClient = Depends(get_nse)) -> dict[str, Any]:
    """Price up to 8 legs from the live chain: payoff at expiry and today, max profit / loss, breakevens,
    probability of profit, net Greeks and the paper-trading margin."""
    symbol = _check(body.symbol, body.expiry)
    data = await _call(nse.chain, symbol, body.expiry, ttl=5 if market_open() else 60)
    lot = await run_in_threadpool(nse.lot_size, symbol, data["expiry"]) or 1
    rows = {r["strike"]: r for r in data["rows"]}
    now = dt.datetime.now(dt.timezone.utc)
    T = om.years_to_expiry(dt.datetime.strptime(data["expiry"], "%d-%b-%Y").date(), now)
    spot = data.get("spot")
    if not spot:
        raise HTTPException(status_code=503, detail="no spot price for this underlying right now")
    legs, detail = [], []
    for leg in body.legs:
        side = (rows.get(leg.strike) or {}).get("ce" if leg.kind == "CE" else "pe")
        if not side or not side.get("ltp"):
            raise HTTPException(status_code=422, detail=f"No traded {leg.kind} at {leg.strike:g} for this expiry")
        g = om.side_greeks(leg.kind, side, spot, leg.strike, T)
        iv = g["iv"] / 100 if g else None
        legs.append(st.Leg(leg.kind, leg.side, leg.strike, leg.lots * lot, side["ltp"], iv))
        detail.append({**leg.model_dump(), "qty": leg.lots * lot, "premium": side["ltp"], "iv": g["iv"] if g else None, "id": side["id"],
                       "symbol": f"OPT:{side['id']}", "label": contract_label(side["id"])})
    atm = rows.get(data["atm"]) or {}
    atm_ivs = [x for x in ((atm.get("ce") or {}).get("iv"), (atm.get("pe") or {}).get("iv")) if x]
    vol = (sum(atm_ivs) / len(atm_ivs) / 100) if atm_ivs else next((leg.iv for leg in legs if leg.iv), 0.15)
    try:
        result = st.analyse(legs, spot, T, vol)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {"symbol": symbol, "expiry": data["expiry"], "spot": spot, "lot_size": lot, "days_to_expiry": round(T * 365, 2),
            "vol_used": round(vol * 100, 2), "legs": detail, "margin": st.short_option_margin(legs), **result}
