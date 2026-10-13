"""India market pulse: FII / DII flows, bulk and block deals, corporate actions and results calendar,
surveillance lists (ASM / GSM / F&O ban), delivery %, IPOs and participant-wise F&O positioning.

All of it is public NSE data (``tools/providers/nse_india.py``). A failed source returns 503 for that
endpoint only; the rest of the page keeps working.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from api import deps
from api.options import get_nse
from core.store import Store
from tools.providers.base import ProviderError
from tools.providers.nse_india import IndiaData

router = APIRouter(prefix="/india", tags=["india"])
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


@lru_cache(maxsize=1)
def get_india() -> IndiaData:
    return IndiaData(get_nse())


def _today() -> dt.date:
    return dt.datetime.now(IST).date()


async def _call(fn, *args):
    try:
        return await run_in_threadpool(fn, *args)
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail=f"NSE: {exc}") from None


def nse_symbol(symbol: str) -> str:
    """RELIANCE.NS / RELIANCE.BO / reliance -> RELIANCE (NSE's own symbol)."""
    return symbol.strip().upper().removesuffix(".NS").removesuffix(".BO")


@router.get("/flows")
async def flows(store: Store = Depends(deps.get_shared_store), india: IndiaData = Depends(get_india)) -> dict[str, Any]:
    """Latest FII / DII cash flows (₹ crore) and the history collected so far."""
    latest = await _call(india.fii_dii)
    if latest.get("date"):
        store.save_flow(latest["date"], latest.get("fii"), latest.get("dii"))
    hist = store.flows(60)
    return {"latest": latest, "history": [{"date": r["day"], "fii": r["fii_net"], "dii": r["dii_net"]} for r in reversed(hist)],
            "note": "Provisional cash-market figures from NSE. History builds up one session at a time while Kairo runs."}


@router.get("/deals")
async def deals(symbol: str | None = Query(None, max_length=30), india: IndiaData = Depends(get_india)) -> dict[str, Any]:
    d = await _call(india.deals)
    if symbol:
        s = nse_symbol(symbol)
        d = {**d, **{k: [x for x in d[k] if x["symbol"] == s] for k in ("bulk", "block", "short")}}
    return d


@router.get("/calendar")
async def calendar(symbols: str | None = Query(None, max_length=4000), days: int = Query(45, ge=1, le=120),
                   india: IndiaData = Depends(get_india)) -> dict[str, Any]:
    """Board meetings (results) and corporate actions (dividends, splits, bonus, buybacks), soonest first."""
    events, actions = await _call(india.events), await _call(india.corporate_actions)
    today, until = _today().isoformat(), (_today() + dt.timedelta(days=days)).isoformat()
    want = {nse_symbol(s) for s in symbols.split(",") if s.strip()} if symbols else None
    items = [{"type": "meeting", "date": e["date"], **e} for e in events] + [{"type": "action", "date": a["ex_date"], **a} for a in actions]
    items = [x for x in items if x["date"] and today <= x["date"] <= until and (want is None or x["symbol"] in want)]
    return {"items": sorted(items, key=lambda x: (x["date"], x["symbol"]))}


@router.get("/surveillance")
async def surveillance(india: IndiaData = Depends(get_india)) -> dict[str, Any]:
    flags, ban = await _call(india.surveillance), await _call(india.fo_ban)
    return {"fo_ban": ban, "items": [{"symbol": s, **f} for s, f in sorted(flags.items())]}


@router.get("/ipos")
async def ipos(india: IndiaData = Depends(get_india)) -> dict[str, Any]:
    return await _call(india.ipos, _today())


def delivery_rows(days: list) -> dict[str, dict[str, Any]]:
    """Latest session per symbol, with its delivery % against the average of the sessions before."""
    if not days:
        return {}
    (last_day, last), prev = days[0], [d for _, d in days[1:]]
    out = {}
    for sym, r in last.items():
        hist = [p[sym] for p in prev if sym in p]
        avg_pct = sum(h["delivery_pct"] or 0 for h in hist) / len(hist) if hist else None
        avg_qty = sum(h["delivery_qty"] for h in hist) / len(hist) if hist else None
        out[sym] = {**r, "date": last_day.isoformat(), "avg_delivery_pct": round(avg_pct, 2) if avg_pct is not None else None,
                    "delivery_x": round(r["delivery_qty"] / avg_qty, 2) if avg_qty else None,
                    "change_pct": round((r["close"] / r["prev_close"] - 1) * 100, 2) if r["close"] and r["prev_close"] else None,
                    "avg_turnover_cr": round(sum(h["turnover_cr"] for h in hist) / len(hist), 2) if hist else r["turnover_cr"]}
    return out


@router.get("/delivery")
async def delivery_scan(min_turnover_cr: float = Query(5, ge=0), limit: int = Query(40, ge=1, le=200),
                        india: IndiaData = Depends(get_india)) -> dict[str, Any]:
    """High-delivery buying: delivery % above 50 and delivered quantity well above its recent average, price up."""
    rows = delivery_rows(await _call(india.bhav, _today()))
    hits = [{"symbol": s, **r} for s, r in rows.items()
            if r["turnover_cr"] >= min_turnover_cr and (r["delivery_pct"] or 0) >= 50 and (r["delivery_x"] or 0) >= 1.5 and (r["change_pct"] or 0) > 0]
    hits.sort(key=lambda r: (r["delivery_x"] or 0) * (r["delivery_pct"] or 0), reverse=True)
    any_row = next(iter(rows.values()), {})
    return {"date": any_row.get("date"), "items": hits[:limit],
            "about": "Delivery % is the share of traded quantity actually taken into demat accounts, not squared off intraday. "
                     "High delivery on a rising day suggests buyers who intend to hold."}


@router.get("/stock/{symbol}")
async def stock(symbol: str, india: IndiaData = Depends(get_india)) -> dict[str, Any]:
    """Everything India-specific about one stock: risk flags, delivery, upcoming events, today's large deals."""
    s, out = nse_symbol(symbol), {"symbol": nse_symbol(symbol), "flags": [], "errors": []}

    async def part(name, fn, *args):
        try:
            return await run_in_threadpool(fn, *args)
        except ProviderError as exc:
            out["errors"].append(f"{name}: {exc}")
            return None

    surv = await part("surveillance", india.surveillance)
    if surv and s in surv:
        f = surv[s]
        if f.get("fo_ban"):
            out["flags"].append({"kind": "fo_ban", "level": "warn", "text": "F&O ban: no new futures or options positions until open interest falls below 80% of the limit."})
        if f.get("asm"):
            out["flags"].append({"kind": "asm", "level": "warn", "text": f"Under {f['asm']} (exchange surveillance): higher margins apply, and price moves are being watched."})
        if f.get("gsm"):
            out["flags"].append({"kind": "gsm", "level": "danger", "text": f"Under GSM ({f['gsm']}): trading is restricted (trade-for-trade, limited frequency or 100% margin). Be very careful."})
    bhav = await part("delivery", india.bhav, _today())
    rows = delivery_rows(bhav) if bhav else {}
    if s in rows:
        r = rows[s]
        out["delivery"] = {k: r[k] for k in ("date", "delivery_pct", "avg_delivery_pct", "delivery_x", "turnover_cr", "avg_turnover_cr")}
        if (r.get("avg_turnover_cr") or 0) < 1:
            out["flags"].append({"kind": "illiquid", "level": "warn", "text": f"Thinly traded: about ₹{r['avg_turnover_cr']:.2f} crore a day. Wide spreads; use limit orders and small sizes."})
    events = await part("calendar", india.events)
    actions = await part("corporate actions", india.corporate_actions)
    today = _today().isoformat()
    out["upcoming"] = sorted([{"type": "meeting", **e} for e in events or [] if e["symbol"] == s and e["date"] and e["date"] >= today]
                             + [{"type": "action", "date": a["ex_date"], **a} for a in actions or [] if a["symbol"] == s and a["ex_date"] and a["ex_date"] >= today],
                             key=lambda x: x["date"])[:6]
    deals_ = await part("deals", india.deals)
    out["deals"] = [{"kind": k, **x} for k in ("bulk", "block") for x in (deals_ or {}).get(k, []) if x["symbol"] == s]
    return out


@router.get("/participants")
async def participants(india: IndiaData = Depends(get_india)) -> dict[str, Any]:
    """Who is long and short index futures and options: FII, DII, pro traders and clients, last few sessions."""
    days = await _call(india.participant_oi, _today())
    return {"days": [{**x, "date": x.get("date") or d.isoformat()} for d, x in days],
            "about": "From NSE's participant-wise open interest (contracts). FII index-futures long % is widely watched: "
                     "below ~30% means FIIs are heavily short, above ~70% heavily long."}
