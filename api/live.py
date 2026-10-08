"""Live price endpoints.

GET /live/{symbol}          -> one intraday snapshot (JSON)
GET /live/{symbol}/stream   -> Server-Sent Events:
    event: snapshot  today's intraday path + latest price (first event; again on polling fallback)
    event: tick      one real-time trade from the Yahoo WebSocket stream
    event: status    upstream stream state changed (connecting / connected / reconnecting / unavailable)
    event: failure   a tool error; ``fatal: true`` (e.g. unknown symbol) means the stream ends

If the upstream stream is down, the endpoint polls a snapshot every ``poll_seconds``
and labels those events ``source: "poll"`` so the UI can show they are delayed.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from core.config import Settings, get_settings
from tools.errors import InvalidInputError, ToolError, UnknownSymbolError
from tools.market_tools import get_live_quote
from tools.models import normalize_symbol
from tools.providers import MarketDataProvider, build_provider
from tools.streaming.yahoo_stream import LiveTick, YahooStreamHub

router = APIRouter(prefix="/live", tags=["live"])

KEEPALIVE_S = 15.0
MAX_STREAM_S = 3600.0  # the browser's EventSource reconnects transparently after this


@lru_cache(maxsize=1)
def get_live_provider() -> MarketDataProvider:
    return build_provider(get_settings())


@lru_cache(maxsize=1)
def get_stream_hub() -> YahooStreamHub | None:
    # Only real market data gets a real-time stream; the mock dataset has none.
    return YahooStreamHub() if get_settings().data_provider == "yahoo" else None


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _symbol_or_422(symbol: str) -> str:
    try:
        return normalize_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


_SNAP_TTL_S = 15.0
_snap_cache: dict[str, tuple[float, dict[str, Any]]] = {}


async def _snapshot(symbol: str, provider: MarketDataProvider, settings: Settings,
                    *, fresh: bool = False) -> dict[str, Any]:
    """Intraday snapshot, cached for a few seconds: streams reconnect whenever the browser's symbol
    set changes, and live ticks take over right after the snapshot anyway."""
    hit = _snap_cache.get(symbol)
    if hit and not fresh and time.monotonic() - hit[0] < _SNAP_TTL_S:
        return hit[1]
    resp = await run_in_threadpool(get_live_quote, symbol, provider=provider,
                                   timeout_s=settings.tool_timeout_seconds)
    data = {**resp.data.model_dump(mode="json"), "source_id": resp.source.source_id,
            "observed_at": resp.observed_at.isoformat(), "fetched_at": resp.fetched_at.isoformat()}
    _snap_cache[symbol] = (time.monotonic(), data)
    return data


def _tick_payload(tick: LiveTick) -> dict[str, Any]:
    return {**tick.model_dump(mode="json"),
            "received_at": dt.datetime.now(dt.timezone.utc).isoformat()}


watch_router = APIRouter(prefix="/watchlist", tags=["live"])
MAX_WATCH_SYMBOLS = 60
SNAPSHOT_CONCURRENCY = 3  # stay polite to Yahoo's rate limits
SPARK_POINTS = 60


def _compact(snap: dict[str, Any]) -> dict[str, Any]:
    """Watchlist rows only need a sparkline, not the full intraday path."""
    points = snap.get("points") or []
    step = max(1, len(points) // SPARK_POINTS)
    spark = [p["price"] for p in points[::step]]
    if points and (not spark or spark[-1] != points[-1]["price"]):
        spark.append(points[-1]["price"])
    keep = ("symbol", "name", "exchange", "currency", "price", "previous_close", "change", "change_pct",
            "market_state", "market_time", "day_high", "day_low", "volume", "session_end")
    return {**{k: snap.get(k) for k in keep}, "spark": spark}


@watch_router.get("/stream")
async def watchlist_stream(
    request: Request,
    symbols: str = Query(..., description="Comma-separated symbols, e.g. AAPL,MSFT,RELIANCE.NS"),
    max_events: int | None = Query(None, ge=1),
    provider: MarketDataProvider = Depends(get_live_provider),
    hub: YahooStreamHub | None = Depends(get_stream_hub),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """One SSE connection for a whole watchlist (browsers cap connections per site).

    Events: ``quote`` (one compact snapshot per symbol), ``tick``, ``status``, ``failure`` (per symbol).
    """
    requested = list(dict.fromkeys(s.strip() for s in symbols.split(",") if s.strip()))
    if not requested or len(requested) > MAX_WATCH_SYMBOLS:
        raise HTTPException(status_code=422, detail=f"give 1-{MAX_WATCH_SYMBOLS} symbols")

    async def events():
        sent = 0
        valid: list[str] = []
        for raw in requested:
            try:
                valid.append(normalize_symbol(raw))
            except ValueError as exc:
                yield _sse("failure", {"symbol": raw, "code": "INVALID_INPUT", "message": str(exc), "fatal": True})

        sem = asyncio.Semaphore(SNAPSHOT_CONCURRENCY)

        async def snap(sym: str):
            async with sem:
                try:
                    return sym, await _snapshot(sym, provider, settings), None
                except ToolError as exc:
                    return sym, None, exc

        live_symbols: list[str] = []
        for sym, data, err in await asyncio.gather(*(snap(s) for s in valid)):
            if data is not None:
                live_symbols.append(sym)
                yield _sse("quote", _compact(data))
            else:
                fatal = isinstance(err, (UnknownSymbolError, InvalidInputError))
                if not fatal:
                    live_symbols.append(sym)  # transient: keep listening for ticks
                yield _sse("failure", {**err.to_dict(), "symbol": sym, "fatal": fatal})

        if hub is None:
            yield _sse("status", {"stream": "unavailable",
                                  "reason": "real-time streaming requires DATA_PROVIDER=yahoo"})
        merged: asyncio.Queue[LiveTick] = asyncio.Queue(maxsize=1000)
        queues = {s: hub.subscribe(s) for s in live_symbols} if hub is not None else {}

        async def forward(queue: asyncio.Queue[LiveTick]) -> None:
            while True:
                tick = await queue.get()
                if merged.full():
                    merged.get_nowait()
                merged.put_nowait(tick)

        forwarders = [asyncio.create_task(forward(q)) for q in queues.values()]
        started = last_beat = time.monotonic()
        last_status: str | None = None
        try:
            while True:
                if await request.is_disconnected():
                    break
                if hub is not None and hub.status != last_status:
                    last_status = hub.status
                    yield _sse("status", {"stream": hub.status, "error": hub.last_error})
                try:
                    tick = await asyncio.wait_for(merged.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    tick = None
                now = time.monotonic()
                if tick is not None:
                    yield _sse("tick", _tick_payload(tick))
                    sent += 1
                    last_beat = now
                if max_events is not None and sent >= max_events:
                    break
                if now - last_beat >= KEEPALIVE_S:
                    yield ": keepalive\n\n"
                    last_beat = now
                if now - started >= MAX_STREAM_S:
                    break
        finally:
            for task in forwarders:
                task.cancel()
            if hub is not None:
                for sym, queue in queues.items():
                    hub.unsubscribe(sym, queue)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/{symbol}")
async def live_snapshot(
    symbol: str,
    provider: MarketDataProvider = Depends(get_live_provider),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    sym = _symbol_or_422(symbol)
    try:
        return await _snapshot(sym, provider, settings)
    except UnknownSymbolError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict()) from None
    except InvalidInputError as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from None
    except ToolError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict()) from None


@router.get("/{symbol}/stream")
async def live_stream(
    symbol: str,
    request: Request,
    poll_seconds: float = Query(5.0, ge=2.0, le=60.0, description="Fallback polling interval"),
    max_events: int | None = Query(None, ge=1, description="Stop after N tick/poll events"),
    provider: MarketDataProvider = Depends(get_live_provider),
    hub: YahooStreamHub | None = Depends(get_stream_hub),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    sym = _symbol_or_422(symbol)

    async def events():
        started = last_beat = last_poll = time.monotonic()
        sent = 0
        try:
            yield _sse("snapshot", {**await _snapshot(sym, provider, settings), "source": "snapshot"})
        except (UnknownSymbolError, InvalidInputError) as exc:
            yield _sse("failure", {**exc.to_dict(), "fatal": True})
            return
        except ToolError as exc:
            yield _sse("failure", {**exc.to_dict(), "fatal": False})

        if hub is None:
            yield _sse("status", {"stream": "unavailable",
                                  "reason": "real-time streaming requires DATA_PROVIDER=yahoo"})
            queue = None
        else:
            queue = hub.subscribe(sym)
        last_status: str | None = None
        try:
            while True:
                if await request.is_disconnected():
                    break
                if hub is not None and hub.status != last_status:
                    last_status = hub.status
                    yield _sse("status", {"stream": hub.status, "error": hub.last_error})

                tick = None
                if queue is not None:
                    try:
                        tick = await asyncio.wait_for(queue.get(), timeout=0.5)
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(0.5)
                now = time.monotonic()

                if tick is not None:
                    yield _sse("tick", _tick_payload(tick))
                    sent += 1
                    last_beat = now
                elif hub is not None and hub.status != "connected" and now - last_poll >= poll_seconds:
                    # Stream down: fall back to polling, clearly labelled as such.
                    last_poll = now
                    try:
                        snap = await _snapshot(sym, provider, settings, fresh=True)
                        yield _sse("snapshot", {**snap, "source": "poll"})
                        sent += 1
                    except ToolError as exc:
                        yield _sse("failure", {**exc.to_dict(), "fatal": False})
                    last_beat = now

                if max_events is not None and sent >= max_events:
                    break
                if now - last_beat >= KEEPALIVE_S:
                    yield ": keepalive\n\n"
                    last_beat = now
                if now - started >= MAX_STREAM_S:
                    break
        finally:
            if queue is not None and hub is not None:
                hub.unsubscribe(sym, queue)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
