"""Query screener and saved scans.

``POST /screener/query`` runs a query (``core/query.py``) over a universe in the background; poll
``GET /screener/query/{id}``. Fields are gathered only as needed: price-based ones from the cached daily
history, India ones from NSE's daily files, and fundamentals only for stocks the cheaper conditions haven't
already ruled out.

Saved scans with ``alert`` on are re-run every 15 minutes during market hours (and once after the close);
stocks that newly match are announced on the alerts stream, like a price alert.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import secrets
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from api import deps
from api.india import IST, delivery_rows, get_india, nse_symbol
from api.insights import load_universe
from api.live import get_live_provider
from api.research import fundamentals_of
from core.config import get_settings
from core.query import EXAMPLES, FIELDS, Query, QueryError, fundamental_fields, parse, technical_fields
from core.store import Store
from core.universes import Universes
from tools.errors import ToolError
from tools.providers.base import ProviderError

log = logging.getLogger("kairo.scans")
router = APIRouter(tags=["screener"])
MAX_SYMBOLS = 500
_JOBS: dict[str, dict[str, Any]] = {}


class QueryIn(BaseModel):
    query: str = Field(max_length=600)
    universe: str | None = Field(None, max_length=80)


class ScanIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    universe: str = Field(max_length=80)
    query: str = Field(max_length=600)
    alert: bool = False


def _parse(text: str) -> Query:
    try:
        return parse(text)
    except QueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


async def _india_rows(q: Query, india) -> tuple[dict[str, dict], dict[str, dict]]:
    if "india" not in q.groups:
        return {}, {}
    today = dt.datetime.now(IST).date()
    try:
        bhav = delivery_rows(await run_in_threadpool(india.bhav, today)) if q.fields & {"delivery_pct", "delivery_x", "turnover_cr"} else {}
    except ProviderError:
        bhav = {}
    try:
        surv = await run_in_threadpool(india.surveillance) if q.fields & {"asm", "gsm", "fo_ban"} else {}
    except ProviderError:
        surv = {}
    return bhav, surv


async def run_query(q: Query, symbols: list[str], provider, india, job: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Rows for the symbols that match: their values for every field the query uses, plus close and day change."""
    settings = get_settings()
    bhav, surv = await _india_rows(q, india)
    need_fund = "fundamental" in q.groups
    out: list[dict[str, Any]] = []

    async def one(sym: str) -> None:
        try:
            h = await deps.history(sym, "2y", provider)
            row: dict[str, Any] = {"symbol": sym, "name": h.name, "currency": h.currency, **await run_in_threadpool(technical_fields, h.bars)}
            if sym.endswith((".NS", ".BO")):
                n = nse_symbol(sym)
                b, s = bhav.get(n), surv.get(n, {})
                row.update({"delivery_pct": b and b["delivery_pct"], "delivery_x": b and b["delivery_x"], "turnover_cr": b and b["turnover_cr"]})
                if surv:
                    row.update({"asm": bool(s.get("asm")), "gsm": bool(s.get("gsm")), "fo_ban": bool(s.get("fo_ban"))})
            verdict = q.evaluate(row)
            if verdict is None and need_fund:  # still undecided: fetch fundamentals for this one only
                try:
                    f = await fundamentals_of(sym, provider, settings, statements=False)
                    row.update(fundamental_fields(f.data))
                except ToolError:
                    pass
                verdict = q.evaluate(row)
            if verdict:
                keep = {"symbol", "name", "currency", "close", "chg_1d"} | q.fields
                out.append({k: v for k, v in row.items() if k in keep})
        except ToolError as exc:
            if job is not None:
                job["errors"][sym] = exc.code
        finally:
            if job is not None:
                job["done"] += 1
    await deps.gather_limited(symbols, one, limit=4 if need_fund else 8)
    return sorted(out, key=lambda r: r["symbol"])


@router.get("/screener/fields")
async def fields() -> dict[str, Any]:
    return {"fields": [{"name": k, "group": g, "unit": u, "about": a} for k, (g, u, a) in FIELDS.items()],
            "examples": [{"name": n, "query": q} for n, q in EXAMPLES]}


@router.post("/screener/query")
async def start_query(body: QueryIn, registry: Universes = Depends(deps.get_universes), provider=Depends(get_live_provider),
                      india=Depends(get_india)) -> dict[str, Any]:
    q = _parse(body.query)
    u = await run_in_threadpool(load_universe, body.universe, registry)
    syms = u.symbols[:MAX_SYMBOLS]
    for k in [k for k, j in _JOBS.items() if time.monotonic() - j["started"] > 1800]:
        _JOBS.pop(k, None)
    jid = secrets.token_urlsafe(8)
    job = _JOBS[jid] = {"id": jid, "query": q.text, "universe": u.key, "label": u.label, "status": "running", "done": 0, "total": len(syms),
                        "rows": [], "errors": {}, "fields": sorted(q.fields), "started": time.monotonic(),
                        "slow": "fundamental" in q.groups}

    async def go() -> None:
        try:
            job["rows"] = await run_query(q, syms, provider, india, job)
            job["status"] = "ready"
        except Exception as exc:  # noqa: BLE001 - surface any failure to the poller
            log.exception("query failed")
            job["status"], job["error"] = "failed", str(exc)
    asyncio.create_task(go())
    return {k: v for k, v in job.items() if k != "started"}


@router.get("/screener/query/{job_id}")
async def query_status(job_id: str) -> dict[str, Any]:
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="That scan has expired; run it again")
    return {k: v for k, v in job.items() if k != "started"}


# ------------------------------------------------------------------------------------------ saved scans + alerts


@router.get("/screener/scans")
async def list_scans(store: Store = Depends(deps.get_store)) -> dict[str, Any]:
    ensure_scan_alerts()
    return {"items": store.scans()}


@router.post("/screener/scans")
async def save_scan(body: ScanIn, scan_id: int | None = None, store: Store = Depends(deps.get_store),
                    registry: Universes = Depends(deps.get_universes)) -> dict[str, Any]:
    _parse(body.query)
    if not registry.known(body.universe):
        raise HTTPException(status_code=404, detail=f"unknown universe {body.universe}")
    sid = store.save_scan(body.name.strip(), body.universe, body.query.strip(), body.alert, scan_id)
    ensure_scan_alerts()
    return next(s for s in store.scans() if s["id"] == sid)


@router.delete("/screener/scans/{scan_id}")
async def delete_scan(scan_id: int, store: Store = Depends(deps.get_store)) -> dict[str, Any]:
    if not store.delete_scan(scan_id):
        raise HTTPException(status_code=404, detail="no such scan")
    return {"deleted": scan_id}


def new_matches(before: list[str], now: list[str]) -> list[str]:
    return [s for s in now if s not in set(before)]


async def run_alert_scans(store: Store, registry: Universes, provider, india, engine) -> int:
    """Re-run every alerting scan once; announce stocks that weren't matching last time. Returns announcements."""
    sent = 0
    for sc in store.scans():
        if not sc["alert"]:
            continue
        try:
            q = parse(sc["query"])
            u = await run_in_threadpool(load_universe, sc["universe"], registry)
            rows = await run_query(q, u.symbols[:MAX_SYMBOLS], provider, india)
        except (QueryError, HTTPException, ToolError, ProviderError) as exc:
            log.info("scan %s skipped: %s", sc["name"], exc)
            continue
        syms = [r["symbol"] for r in rows]
        fresh = new_matches(sc["last_symbols"], syms) if sc["last_run"] else []  # the first run sets the baseline
        store.scan_ran(sc["id"], syms)
        if fresh:
            names = ", ".join(s.split(".")[0] for s in fresh[:6]) + (f" +{len(fresh) - 6} more" if len(fresh) > 6 else "")
            engine._publish({"type": "triggered", "kind": "scan", "scan": {"id": sc["id"], "name": sc["name"], "symbols": fresh},
                             "alert": {"id": f"scan-{sc['id']}", "symbol": fresh[0]},
                             "message": f"{len(fresh)} new match{'es' if len(fresh) > 1 else ''}: {names}"})
            sent += 1
    return sent


_TASK: asyncio.Task | None = None


def _due(now: dt.datetime, last: dt.datetime | None) -> bool:
    t = now.astimezone(IST)
    if t.weekday() >= 5:
        return False
    in_session = dt.time(9, 20) <= t.time() <= dt.time(15, 30)
    after_close = dt.time(15, 45) <= t.time() <= dt.time(16, 30) and (last is None or last.astimezone(IST).time() < dt.time(15, 45) or last.date() != now.date())
    return (in_session and (last is None or now - last >= dt.timedelta(minutes=15))) or after_close


async def _loop() -> None:
    last: dt.datetime | None = None
    while True:
        try:
            now = dt.datetime.now(dt.timezone.utc)
            if _due(now, last):
                last = now
                for store in deps.all_stores():  # every account with alerting scans
                    if any(s["alert"] for s in store.scans()):
                        await run_alert_scans(store, deps.get_universes(), get_live_provider(), get_india(), deps.alert_engine_for(store))
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("scan alerts failed")
        await asyncio.sleep(60)


def ensure_scan_alerts() -> None:
    global _TASK
    if _TASK is None or _TASK.done():
        _TASK = asyncio.get_running_loop().create_task(_loop(), name="scan-alerts")
