"""Price alerts: definitions, evaluation and a background engine.

The engine lives in the API process. It listens to the shared real-time tick stream
for every symbol with an active alert, and also polls a snapshot periodically (often
when the stream is down, rarely when it is up) so alerts still fire for closed markets
or the mock provider. An alert fires once (status -> triggered) and is announced to
every connected listener; it can be re-armed from the UI.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from core.store import Store
from tools.errors import ToolError
from tools.market_tools import get_live_quote
from tools.models import Symbol

log = logging.getLogger(__name__)

AlertKind = Literal["price_above", "price_below", "day_change_above", "day_change_below"]
KIND_LABEL = {
    "price_above": "Price rises to or above", "price_below": "Price falls to or below",
    "day_change_above": "Day change at or above (%)", "day_change_below": "Day change at or below (%)",
}


class AlertIn(BaseModel):
    symbol: Symbol
    kind: AlertKind
    threshold: float
    note: str = Field("", max_length=140)

    @model_validator(mode="after")
    def _sane(self) -> AlertIn:
        if self.kind.startswith("price") and self.threshold <= 0:
            raise ValueError("price threshold must be positive")
        if self.kind.startswith("day_change") and not -100 < self.threshold < 1000:
            raise ValueError("day-change threshold must be a percentage between -100 and 1000")
        return self


class Alert(AlertIn):
    id: int
    status: Literal["active", "triggered"]
    created_at: dt.datetime
    triggered_at: dt.datetime | None = None
    triggered_price: float | None = None
    triggered_value: float | None = None


def evaluate(kind: str, threshold: float, price: float, change_pct: float | None) -> float | None:
    """The observed value if the alert condition holds, else None."""
    if kind == "price_above" and price >= threshold:
        return price
    if kind == "price_below" and price <= threshold:
        return price
    if change_pct is not None:
        if kind == "day_change_above" and change_pct >= threshold:
            return change_pct
        if kind == "day_change_below" and change_pct <= threshold:
            return change_pct
    return None


def describe(alert: Alert | dict[str, Any]) -> str:
    a = alert if isinstance(alert, dict) else alert.model_dump()
    unit = "%" if a["kind"].startswith("day_change") else ""
    return f"{a['symbol']}: {KIND_LABEL[a['kind']].split(' (')[0].lower()} {a['threshold']:g}{unit}"


class AlertEngine:
    def __init__(self, store: Store, provider: Any, hub: Any, *, timeout_s: float = 8.0,
                 poll_when_streaming_s: float = 120.0, poll_without_stream_s: float = 20.0):
        self.store, self.provider, self.hub = store, provider, hub
        self.timeout_s = timeout_s
        self.poll_streaming, self.poll_fallback = poll_when_streaming_s, poll_without_stream_s
        self._listeners: set[asyncio.Queue[dict[str, Any]]] = set()
        self._queues: dict[str, asyncio.Queue] = {}
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._last_poll: dict[str, float] = {}
        self.last_check: dt.datetime | None = None

    # -- lifecycle ---------------------------------------------------------------
    def ensure_running(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="alert-engine")

    def poke(self) -> None:
        """Re-read alerts now (after create / delete / re-arm)."""
        self._wake.set()

    # -- listeners ---------------------------------------------------------------
    def listen(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._listeners.add(q)
        return q

    def unlisten(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._listeners.discard(q)

    def _publish(self, event: dict[str, Any]) -> None:
        for q in list(self._listeners):
            if q.full():
                q.get_nowait()
            q.put_nowait(event)

    # -- evaluation ---------------------------------------------------------------
    async def evaluate_symbol(self, symbol: str, price: float, change_pct: float | None, at: dt.datetime) -> int:
        fired = 0
        for row in await asyncio.to_thread(self.store.list_alerts, "active"):
            if row["symbol"] != symbol:
                continue
            value = evaluate(row["kind"], row["threshold"], price, change_pct)
            if value is None:
                continue
            if await asyncio.to_thread(self.store.trigger_alert, row["id"], price, value, at):
                fired += 1
                alert = Alert(**{**row, "status": "triggered", "triggered_at": at, "triggered_price": price,
                                 "triggered_value": value})
                self._publish({"type": "triggered", "alert": alert.model_dump(mode="json"),
                               "message": f"{describe(alert)} — now {price:,.2f}"})
        return fired

    async def _snapshot(self, symbol: str) -> None:
        try:
            resp = await asyncio.to_thread(get_live_quote, symbol, provider=self.provider, timeout_s=self.timeout_s)
        except ToolError as exc:
            log.info("alert snapshot for %s failed: %s", symbol, exc)
            return
        q = resp.data
        await self.evaluate_symbol(symbol, q.price, q.change_pct, q.market_time)

    async def run_once(self) -> None:
        """One engine cycle: sync subscriptions, apply queued ticks, poll due snapshots."""
        active = await asyncio.to_thread(self.store.list_alerts, "active")
        symbols = {a["symbol"] for a in active}
        if self.hub is not None:
            for sym in symbols - set(self._queues):
                self._queues[sym] = self.hub.subscribe(sym)
            for sym in set(self._queues) - symbols:
                self.hub.unsubscribe(sym, self._queues.pop(sym))
        for sym, q in list(self._queues.items()):
            latest = None
            while not q.empty():
                latest = q.get_nowait()
            if latest is not None:
                await self.evaluate_symbol(sym, latest.price, latest.change_percent, latest.time)
        streaming = self.hub is not None and getattr(self.hub, "status", "") == "connected"
        interval = self.poll_streaming if streaming else self.poll_fallback
        now = time.monotonic()
        due = [s for s in symbols if now - self._last_poll.get(s, -1e9) >= interval]
        for s in due:
            self._last_poll[s] = now
        if due:
            sem = asyncio.Semaphore(3)

            async def one(sym: str) -> None:
                async with sem:
                    await self._snapshot(sym)
            await asyncio.gather(*(one(s) for s in due))
        self.last_check = dt.datetime.now(dt.timezone.utc)

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the engine must keep running
                log.exception("alert engine cycle failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
