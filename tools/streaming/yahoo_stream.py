"""Real-time price ticks from Yahoo Finance's WebSocket streamer.

One upstream connection is shared by every listener. Each raw message is
decoded and validated into a ``LiveTick`` (the same contract idea as the
request/response tools: typed, validated, timestamped at the source); a
message that fails validation is dropped and counted, never forwarded.

Reliability:
  * reconnects with exponential backoff (1 s .. 30 s) and resubscribes;
  * resubscribes periodically (Yahoo can silently stop sending otherwise);
  * forces IPv4 (IPv6 routes to the streamer can hang on some networks);
  * each listener has a bounded queue; a slow listener loses its *oldest*
    ticks, never blocks the others;
  * the upstream connection closes when the last listener leaves.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import socket
from collections.abc import Callable
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, Field, ValidationError

from tools.streaming.protobuf_lite import ProtobufDecodeError, decode_pricing_message

log = logging.getLogger(__name__)

STREAM_URL = "wss://streamer.finance.yahoo.com/?version=2"
_SESSIONS = {0: "pre-market", 1: "regular", 2: "post-market", 3: "extended"}


class LiveTick(BaseModel):
    symbol: str
    price: float = Field(gt=0)
    time: AwareDatetime
    change: float | None = None
    change_percent: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    day_volume: int | None = None
    previous_close: float | None = None
    session: str | None = None
    exchange: str | None = None
    currency: str | None = None
    source: Literal["yahoo_stream"] = "yahoo_stream"


class MalformedTick(ValueError):
    pass


def _r(value: float | None) -> float | None:
    # Floats arrive as float32 (1193.300049); 4 decimals removes the artefact.
    return None if value is None else round(value, 4)


def parse_tick(raw: str | bytes) -> LiveTick:
    """Decode one streamer frame into a validated tick (raises ``MalformedTick``)."""
    try:
        envelope = json.loads(raw)
        if envelope.get("type") != "pricing":
            raise MalformedTick(f"unexpected frame type {envelope.get('type')!r}")
        data = decode_pricing_message(envelope["message"])
        time_ms = data["time"]
        return LiveTick(
            symbol=data["id"],
            price=_r(data["price"]),
            time=dt.datetime.fromtimestamp(time_ms / 1000, tz=dt.timezone.utc),
            change=_r(data.get("change")),
            change_percent=_r(data.get("change_percent")),
            day_high=_r(data.get("day_high")),
            day_low=_r(data.get("day_low")),
            day_volume=data.get("day_volume"),
            previous_close=_r(data.get("previous_close")),
            session=_SESSIONS.get(data.get("market_hours", -1)),
            exchange=data.get("exchange"),
            currency=data.get("currency"),
        )
    except MalformedTick:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, ProtobufDecodeError,
            ValidationError, OverflowError, OSError) as exc:
        raise MalformedTick(f"{type(exc).__name__}: {exc}") from exc


StreamStatus = Literal["idle", "connecting", "connected", "reconnecting"]


class YahooStreamHub:
    def __init__(
        self,
        *,
        url: str = STREAM_URL,
        connect: Callable[..., Any] | None = None,
        queue_size: int = 200,
        resubscribe_every_s: float = 120.0,
        max_backoff_s: float = 30.0,
    ):
        if connect is None:
            import websockets

            def connect(url: str) -> Any:  # noqa: F811 - default factory
                return websockets.connect(url, open_timeout=20, ping_interval=20,
                                          family=socket.AF_INET, max_queue=1024)
        self._connect = connect
        self._url = url
        self._queue_size = queue_size
        self._resubscribe_every = resubscribe_every_s
        self._max_backoff = max_backoff_s
        self._listeners: dict[str, set[asyncio.Queue[LiveTick]]] = {}
        self._task: asyncio.Task[None] | None = None
        self._ws: Any = None
        self.status: StreamStatus = "idle"
        self.last_error: str | None = None
        self.malformed_frames = 0
        self.ticks_received = 0

    # -- listener API --------------------------------------------------------

    def subscribe(self, symbol: str) -> asyncio.Queue[LiveTick]:
        queue: asyncio.Queue[LiveTick] = asyncio.Queue(maxsize=self._queue_size)
        is_new = symbol not in self._listeners
        self._listeners.setdefault(symbol, set()).add(queue)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="yahoo-stream")
        elif is_new and self._ws is not None:
            asyncio.create_task(self._send_subscribe([symbol]))
        return queue

    def unsubscribe(self, symbol: str, queue: asyncio.Queue[LiveTick]) -> None:
        queues = self._listeners.get(symbol)
        if queues is None:
            return
        queues.discard(queue)
        if not queues:
            del self._listeners[symbol]
            if self._ws is not None:
                if self._listeners:
                    asyncio.create_task(self._send({"unsubscribe": [symbol]}))
                else:
                    asyncio.create_task(self._ws.close())  # last listener gone: drop upstream

    @property
    def symbols(self) -> list[str]:
        return sorted(self._listeners)

    # -- upstream --------------------------------------------------------------

    async def _send(self, payload: dict[str, Any]) -> None:
        try:
            if self._ws is not None:
                await self._ws.send(json.dumps(payload))
        except Exception as exc:  # noqa: BLE001 - connection loop will recover
            log.debug("stream send failed: %s", exc)

    async def _send_subscribe(self, symbols: list[str]) -> None:
        if symbols:
            await self._send({"subscribe": symbols})

    def _dispatch(self, tick: LiveTick) -> None:
        for queue in self._listeners.get(tick.symbol, ()):
            if queue.full():
                queue.get_nowait()  # drop the oldest tick for this slow listener
            queue.put_nowait(tick)

    async def _resubscribe_loop(self) -> None:
        while True:
            await asyncio.sleep(self._resubscribe_every)
            await self._send_subscribe(self.symbols)

    async def _run(self) -> None:
        backoff = 1.0
        while self._listeners:
            self.status = "connecting" if self.status == "idle" else "reconnecting"
            resub: asyncio.Task[None] | None = None
            try:
                async with self._connect(self._url) as ws:
                    self._ws = ws
                    self.status, self.last_error, backoff = "connected", None, 1.0
                    await self._send_subscribe(self.symbols)
                    resub = asyncio.create_task(self._resubscribe_loop())
                    async for raw in ws:
                        if not self._listeners:
                            break
                        try:
                            tick = parse_tick(raw)
                        except MalformedTick as exc:
                            self.malformed_frames += 1
                            log.debug("dropped malformed stream frame: %s", exc)
                            continue
                        self.ticks_received += 1
                        self._dispatch(tick)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any network failure -> reconnect
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("Yahoo stream disconnected: %s", self.last_error)
            finally:
                self._ws = None
                if resub is not None:
                    resub.cancel()
            if self._listeners:
                self.status = "reconnecting"
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff)
        self.status = "idle"
