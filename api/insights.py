"""News, alerts and screener APIs."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from api import deps
from api.live import get_live_provider
from core.alerts import Alert, AlertEngine, AlertIn
from core.screener import screen_row
from core.universes import Universe, Universes
from core.store import Store
from tools.errors import ToolError, UnknownSymbolError
from tools.models import normalize_symbol
from tools.providers import MarketDataProvider
from tools.providers.listings import ListingUnavailable
from tools.providers.news_images import NewsImageStore, proxied
from tools.providers.news_reader import NewsReader, article_allowed, filing_allowed

router = APIRouter(tags=["insights"])
MAX_SYMBOLS = 40


def _symbols(raw: str, limit: int = MAX_SYMBOLS) -> tuple[list[str], list[str]]:
    ok, bad = [], []
    for s in dict.fromkeys(x.strip() for x in raw.split(",") if x.strip()):
        try:
            ok.append(normalize_symbol(s))
        except ValueError:
            bad.append(s)
    if not ok or len(ok) > limit:
        raise HTTPException(status_code=422, detail=f"give 1-{limit} valid symbols")
    return ok, bad


# ---------------------------------------------------------------------------- news


def _publisher(category: str) -> str:
    """"news: Reuters" -> "Reuters"; "filing: BSE · Board Meeting" -> "BSE filing · Board Meeting"."""
    if category.startswith("filing: "):
        source, _, rest = category.removeprefix("filing: ").partition(" · ")
        return f"{source} filing" + (f" · {rest}" if rest else "")
    return category.removeprefix("news: ")


@router.get("/news")
async def news(symbols: str = Query(...), period: str = Query("7d", pattern=r"^(7d|30d|90d)$"),
               provider: MarketDataProvider = Depends(get_live_provider)) -> dict[str, Any]:
    """Merged, de-duplicated headlines for many symbols, newest first."""
    syms, bad = _symbols(symbols)
    results = await deps.gather_limited(syms, lambda s: deps.news(s, period, provider))
    merged: dict[str, dict[str, Any]] = {}
    errors = {s: "INVALID_INPUT" for s in bad}
    for sym, res in zip(syms, results):
        if isinstance(res, ToolError):
            errors[sym] = res.code
            continue
        if isinstance(res, BaseException):
            raise res
        for item in res.items:
            key = item.url or item.id
            if key in merged:
                merged[key]["symbols"].append(sym)
                continue
            merged[key] = {"id": item.id, "title": item.title, "url": item.url, "published_at": item.published_at,
                           "publisher": _publisher(item.category), "kind": "filing" if item.category.startswith("filing:") else "news",
                           "summary": item.summary,
                           "image": proxied(item.image_url), "thumb": proxied(item.thumbnail_url or item.image_url),
                           "symbols": [sym]}
    items = sorted(merged.values(), key=lambda i: i["published_at"], reverse=True)
    return {"items": [{**i, "published_at": i["published_at"].isoformat()} for i in items], "errors": errors,
            "period": period, "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat()}


@router.get("/news/preview")
async def news_preview(u: str = Query(..., max_length=2000), reader: NewsReader = Depends(deps.get_news_reader)) -> dict[str, Any]:
    """What a link preview shows: headline, picture and the publisher's own short description."""
    if filing_allowed(u):
        return {"url": u, "kind": "filing", "pdf": f"/news/filing?u={quote(u, safe='')}"}
    if not article_allowed(u):
        raise HTTPException(status_code=404, detail="no preview for this link")
    data = await reader.preview(u)
    if data is None:
        raise HTTPException(status_code=503, detail="the publisher's page couldn't be loaded")
    return {"url": u, "kind": "news", **data, "image": proxied(data.get("image"))}


@router.get("/news/filing")
async def news_filing(u: str = Query(..., max_length=2000), reader: NewsReader = Depends(deps.get_news_reader)) -> Response:
    """A BSE filing PDF, relayed so it opens inside the app (BSE forbids framing its own pages)."""
    pdf = await reader.filing_pdf(u)
    if pdf is None:
        raise HTTPException(status_code=404, detail="filing unavailable")
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": "inline", "Cache-Control": "public, max-age=2592000, immutable",
                             "X-Content-Type-Options": "nosniff"})


@router.get("/news/image")
async def news_image(u: str = Query(..., max_length=2000),
                     images: NewsImageStore = Depends(deps.get_news_images)) -> Response:
    """A news picture, fetched from the provider's image host once and then served from disk."""
    found = await images.get(u)
    if found is None:
        raise HTTPException(status_code=404, detail="image unavailable")
    return Response(found.content, media_type=found.content_type,
                    headers={"Cache-Control": "public, max-age=2592000, immutable", "X-Content-Type-Options": "nosniff"})


# ---------------------------------------------------------------------------- alerts


def _alert(row: dict[str, Any]) -> dict[str, Any]:
    return Alert(**row).model_dump(mode="json")


# Alert endpoints are async: starting / poking the engine needs the running event loop.
@router.get("/alerts")
async def list_alerts(store: Store = Depends(deps.get_store),
                engine: AlertEngine = Depends(deps.get_alert_engine)) -> dict[str, Any]:
    engine.ensure_running()
    stream = getattr(engine.hub, "status", None) if engine.hub is not None else "unavailable"
    return {"alerts": [_alert(r) for r in store.list_alerts()], "engine": {
        "last_check": engine.last_check.isoformat() if engine.last_check else None, "stream": stream}}


@router.post("/alerts", status_code=201)
async def create_alert(body: AlertIn, store: Store = Depends(deps.get_store),
                       engine: AlertEngine = Depends(deps.get_alert_engine),
                       provider: MarketDataProvider = Depends(get_live_provider)) -> dict[str, Any]:
    try:
        await deps.quote(body.symbol, provider)
    except UnknownSymbolError:
        raise HTTPException(status_code=404, detail=f"Unknown symbol {body.symbol}") from None
    except ToolError:
        pass  # data source hiccup: keep the alert, the engine will evaluate it later
    new_id = store.add_alert(body.symbol, body.kind, body.threshold, body.note)
    engine.ensure_running()
    engine.poke()
    return _alert(store.get_alert(new_id))  # type: ignore[arg-type]


@router.delete("/alerts/{alert_id}", status_code=204)
async def delete_alert(alert_id: int, store: Store = Depends(deps.get_store),
                 engine: AlertEngine = Depends(deps.get_alert_engine)) -> None:
    if not store.delete_alert(alert_id):
        raise HTTPException(status_code=404, detail="no such alert")
    engine.poke()


@router.post("/alerts/{alert_id}/rearm")
async def rearm_alert(alert_id: int, store: Store = Depends(deps.get_store),
                engine: AlertEngine = Depends(deps.get_alert_engine)) -> dict[str, Any]:
    if not store.rearm_alert(alert_id):
        raise HTTPException(status_code=404, detail="no such alert")
    engine.poke()
    return _alert(store.get_alert(alert_id))  # type: ignore[arg-type]


@router.get("/alerts/stream")
async def alert_stream(request: Request, max_events: int | None = Query(None, ge=1),
                       engine: AlertEngine = Depends(deps.get_alert_engine)) -> StreamingResponse:
    engine.ensure_running()

    async def events():
        q = engine.listen()
        sent, last_beat = 0, time.monotonic()
        try:
            yield ": connected\n\n"
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(q.get(), timeout=1.0)
                    yield f"event: {event['type']}\ndata: {json.dumps(event, default=str)}\n\n"
                    sent += 1
                    last_beat = time.monotonic()
                    if max_events and sent >= max_events:
                        break
                except asyncio.TimeoutError:
                    if time.monotonic() - last_beat > 15:
                        yield ": keepalive\n\n"
                        last_beat = time.monotonic()
        finally:
            engine.unlisten(q)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------- screener


class _ScreenJob:
    def __init__(self, key: str, symbols: list[str]):
        self.key, self.symbols = key, symbols
        self.status, self.done, self.rows, self.errors = "running", 0, [], {}
        self.started = time.monotonic()
        self.computed_at: dt.datetime | None = None

    def as_dict(self, label: str) -> dict[str, Any]:
        return {"key": self.key, "label": label, "status": self.status, "done": self.done,
                "total": len(self.symbols), "rows": self.rows, "errors": self.errors,
                "computed_at": self.computed_at.isoformat() if self.computed_at else None}


_JOBS: dict[str, _ScreenJob] = {}
_READY: dict[str, _ScreenJob] = {}  # last finished scan per key, shown while a refresh runs
_JOB_TTL_S = 30 * 60
SCAN_CONCURRENCY = 8


async def _run_screen(job: _ScreenJob, provider: MarketDataProvider) -> None:
    async def one(sym: str) -> None:
        try:
            h = await deps.history(sym, "2y", provider)  # same cache entry as Explore's screens
            row = await run_in_threadpool(screen_row, sym, h.name, h.currency, h.bars)
            if row:
                job.rows.append(row.model_dump())
        except ToolError as exc:
            job.errors[sym] = exc.code
        finally:
            job.done += 1
    await deps.gather_limited(job.symbols, one, limit=SCAN_CONCURRENCY)
    job.status, job.computed_at = "ready", dt.datetime.now(dt.timezone.utc)
    _READY[job.key] = job


def load_universe(key: str | None, registry: Universes) -> Universe:
    """Shared by the screener and Explore: 404 for an unknown key, 503 when its list can't be downloaded."""
    key = key or registry.default_key
    if not registry.known(key):
        raise HTTPException(status_code=404, detail=f"unknown universe {key}")
    try:
        return registry.get(key)
    except ListingUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@router.get("/screener/universes")
def universes(registry: Universes = Depends(deps.get_universes)) -> dict[str, Any]:
    return {"default": registry.default_key, "items": registry.catalog()}


@router.get("/stocks/search")
async def stock_search(q: str = Query("", max_length=60), limit: int = Query(10, ge=1, le=50),
                       registry: Universes = Depends(deps.get_universes)) -> dict[str, Any]:
    """Every NSE stock and ETF, every US-listed stock, and the main coins, from the official lists."""
    try:
        hits = await run_in_threadpool(registry.search, q, limit)
    except ListingUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return {"query": q, "items": [h.__dict__ for h in hits]}


@router.get("/stocks/venues/{symbol}")
async def stock_venues(symbol: str, registry: Universes = Depends(deps.get_universes)) -> dict[str, Any]:
    """Where an Indian company trades: {"NSE": "M&M.NS", "BSE": "M&M.BO", "isin": ..., "bse_code": "500520"}."""
    try:
        sym = normalize_symbol(symbol)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"invalid symbol {symbol!r}") from None
    return {"symbol": sym, **await run_in_threadpool(registry.venues, sym)}


@router.get("/screener")
async def screener(universe: str | None = Query(None), symbols: str | None = Query(None),
                   refresh: bool = Query(False), registry: Universes = Depends(deps.get_universes),
                   provider: MarketDataProvider = Depends(get_live_provider)) -> dict[str, Any]:
    """Returns the current scan state immediately; poll until ``status == "ready"``."""
    if symbols:
        syms, _ = _symbols(symbols, limit=60)
        key, label = "custom:" + ",".join(syms), "Custom list"
    else:
        u = await run_in_threadpool(load_universe, universe, registry)
        key, label, syms = u.key, u.label, u.symbols
    job = _JOBS.get(key)
    stale = job is not None and job.status == "ready" and time.monotonic() - job.started > _JOB_TTL_S
    if job is None or stale or (refresh and job.status == "ready"):
        job = _JOBS[key] = _ScreenJob(key, syms)
        asyncio.create_task(_run_screen(job, provider))
    prev = _READY.get(key)
    if job.status == "running" and prev is not None:  # stale-while-revalidate: keep showing the last scan
        return {**prev.as_dict(label), "status": "refreshing", "done": job.done, "total": len(job.symbols)}
    return job.as_dict(label)
