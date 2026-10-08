"""Real-time feed: protobuf decoding, tick validation, stream hub, live endpoints."""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import struct

import httpx
import pytest
from fastapi.testclient import TestClient

from api.live import get_live_provider, get_stream_hub
from api.main import app
from tools import DataSourceUnavailableError
from tools.market_tools import get_live_quote
from tools.providers.yahoo_provider import BASE_URL, YahooFinanceProvider
from tools.streaming.protobuf_lite import ProtobufDecodeError, decode, decode_pricing_message
from tools.streaming.yahoo_stream import LiveTick, MalformedTick, YahooStreamHub, parse_tick

# A real frame captured from Yahoo's streamer (RELIANCE.NS, 2026-09-30 06:16:08 UTC).
CAPTURED = "CgtSRUxJQU5DRS5OUxWaCZVEGICRpo+eaCoDTlNJMAg4AUWhFF8/SMbtzAVlAM0kQbABDtgBBPUCAM0kQf0CoRRfPw=="


# --- minimal encoder used only to build test frames ---------------------------------------


def _varint(n: int) -> bytes:
    out = b""
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out += bytes([byte | 0x80])
        else:
            return out + bytes([byte])


def encode_pricing(symbol: str, price: float, time_ms: int, change: float = 1.5) -> str:
    buf = b"\x0a" + _varint(len(symbol)) + symbol.encode()               # 1: id
    buf += b"\x15" + struct.pack("<f", price)                              # 2: price
    buf += b"\x18" + _varint((time_ms << 1) ^ (time_ms >> 63))            # 3: time (sint64)
    buf += b"\x38" + _varint(1)                                            # 7: market_hours
    buf += b"\x65" + struct.pack("<f", change)                             # 12: change
    return base64.b64encode(buf).decode()


def frame(symbol="AAPL", price=230.25, time_ms=1790748968000) -> str:
    return json.dumps({"type": "pricing", "message": encode_pricing(symbol, price, time_ms)})


# --- decoding ------------------------------------------------------------------------------


def test_decodes_real_captured_frame():
    d = decode_pricing_message(CAPTURED)
    assert d["id"] == "RELIANCE.NS"
    assert d["price"] == pytest.approx(1192.3, abs=1e-3)
    assert dt.datetime.fromtimestamp(d["time"] / 1000, dt.timezone.utc) == dt.datetime(
        2026, 9, 30, 6, 16, 8, tzinfo=dt.timezone.utc)
    assert d["market_hours"] == 1


def test_roundtrip_and_unknown_fields_are_skipped():
    raw = base64.b64decode(encode_pricing("MSFT", 510.5, 1790748968123))
    raw += b"\xa8\x06" + _varint(7)  # field 101, varint: unknown, must be ignored
    d = decode(raw)
    assert d["id"] == "MSFT" and d["price"] == pytest.approx(510.5) and d["time"] == 1790748968123


def test_truncated_frame_raises():
    with pytest.raises(ProtobufDecodeError):
        decode(base64.b64decode(encode_pricing("MSFT", 1.0, 1))[:-2])


def test_parse_tick_validates():
    tick = parse_tick(frame("AAPL", 230.25))
    assert isinstance(tick, LiveTick) and tick.symbol == "AAPL" and tick.price == 230.25
    assert tick.session == "regular" and tick.time.tzinfo is not None


@pytest.mark.parametrize("raw", [
    "not json",
    json.dumps({"type": "heartbeat"}),
    json.dumps({"type": "pricing", "message": "!!!notbase64"}),
    json.dumps({"type": "pricing", "message": encode_pricing("AAPL", -5.0, 1790748968000)}),  # price <= 0
])
def test_parse_tick_rejects_malformed(raw):
    with pytest.raises(MalformedTick):
        parse_tick(raw)


# --- stream hub ------------------------------------------------------------------------------


class FakeSocket:
    def __init__(self, frames: list[str]):
        self.frames, self.sent = frames, []

    async def send(self, msg: str) -> None:
        self.sent.append(json.loads(msg))

    async def close(self) -> None:
        self.frames = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        if not self.frames:
            await asyncio.sleep(3600)  # idle like a real socket
        return self.frames.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def test_hub_dispatches_valid_ticks_by_symbol_and_drops_malformed():
    sock = FakeSocket([frame("AAPL", 230.0), "garbage", frame("MSFT", 510.0), frame("AAPL", 231.0)])

    async def scenario():
        hub = YahooStreamHub(connect=lambda url: sock)
        q_aapl = hub.subscribe("AAPL")
        q_msft = hub.subscribe("MSFT")
        got = [await asyncio.wait_for(q_aapl.get(), 1), await asyncio.wait_for(q_aapl.get(), 1)]
        other = await asyncio.wait_for(q_msft.get(), 1)
        assert hub.status == "connected"
        hub.unsubscribe("AAPL", q_aapl)
        hub.unsubscribe("MSFT", q_msft)
        await asyncio.sleep(0.05)
        return hub, got, other

    hub, got, other = asyncio.run(scenario())
    assert [t.price for t in got] == [230.0, 231.0]
    assert other.symbol == "MSFT"
    assert hub.malformed_frames == 1
    assert {"subscribe": ["AAPL"]} in sock.sent or {"subscribe": ["AAPL", "MSFT"]} in sock.sent


def test_hub_reconnects_after_failure():
    attempts = []

    class Boom:
        async def __aenter__(self):
            attempts.append(1)
            raise OSError("network down")

        async def __aexit__(self, *exc):
            return False

    sock = FakeSocket([frame("AAPL", 229.0)])

    def connect(url):
        return Boom() if not attempts else sock

    async def scenario():
        hub = YahooStreamHub(connect=connect)
        hub._max_backoff = 0.01  # keep the test fast
        q = hub.subscribe("AAPL")
        tick = await asyncio.wait_for(q.get(), 5)
        hub.unsubscribe("AAPL", q)
        return hub, tick

    hub, tick = asyncio.run(scenario())
    assert tick.price == 229.0 and len(attempts) == 1


# --- snapshot tool (Yahoo, fake transport) ---------------------------------------------------


def _live_payload():
    now = dt.datetime.now(dt.timezone.utc)
    t = [int((now - dt.timedelta(minutes=m)).timestamp()) for m in (3, 2, 1)]
    return {"chart": {"error": None, "result": [{
        "meta": {"symbol": "AAPL", "currency": "USD", "regularMarketPrice": 231.5,
                 "regularMarketTime": int(now.timestamp()) - 5, "chartPreviousClose": 229.0,
                 "regularMarketDayHigh": 232.0, "regularMarketDayLow": 228.5,
                 "currentTradingPeriod": {"regular": {"start": t[0] - 600, "end": t[2] + 3600}}},
        "timestamp": t, "indicators": {"quote": [{"close": [230.0, None, 231.0]}]}}]}}


def test_live_quote_snapshot():
    client = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=json.dumps(_live_payload()))))
    resp = get_live_quote("AAPL", provider=YahooFinanceProvider(client=client), timeout_s=2)
    q = resp.data
    assert q.price == 231.5 and q.market_state == "open"
    assert q.change == pytest.approx(2.5) and q.change_pct == pytest.approx(1.0917, abs=1e-3)
    assert [p.price for p in q.points] == [230.0, 231.0, 231.5]  # null minute skipped, last trade appended
    assert resp.observed_at == q.market_time


def test_live_quote_falls_back_to_second_host_on_429():
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        if request.url.host == "query1.finance.yahoo.com":
            return httpx.Response(429)
        return httpx.Response(200, content=json.dumps(_live_payload()))

    client = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    get_live_quote("AAPL", provider=YahooFinanceProvider(client=client), timeout_s=2)
    assert hosts[:2] == ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]  # (then the daily-close lookup)


def test_live_quote_both_hosts_rate_limited():
    client = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(lambda r: httpx.Response(429)))
    with pytest.raises(DataSourceUnavailableError):
        get_live_quote("AAPL", provider=YahooFinanceProvider(client=client), timeout_s=2)


# --- HTTP endpoints ---------------------------------------------------------------------------


class FakeHub:
    status, last_error = "connected", None

    def __init__(self, ticks: list[LiveTick]):
        self.ticks, self.unsubscribed = ticks, []

    def subscribe(self, symbol):
        q: asyncio.Queue = asyncio.Queue()
        for t in self.ticks:
            q.put_nowait(t)
        return q

    def unsubscribe(self, symbol, queue):
        self.unsubscribed.append(symbol)


@pytest.fixture
def live_client(mock_provider):
    hub = FakeHub([parse_tick(frame("AAPL", 247.0)), parse_tick(frame("AAPL", 246.5))])
    app.dependency_overrides[get_live_provider] = lambda: mock_provider
    app.dependency_overrides[get_stream_hub] = lambda: hub
    yield TestClient(app), hub
    app.dependency_overrides.clear()


def read_events(response) -> list[tuple[str, dict]]:
    events, name = [], None
    for line in response.iter_lines():
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            events.append((name, json.loads(line[5:])))
    return events


def test_snapshot_endpoint(live_client):
    client, _ = live_client
    body = client.get("/live/AAPL").json()
    assert body["price"] == 246.1 and body["market_state"] == "closed" and body["source_id"]
    assert client.get("/live/ZZZZ").status_code == 404
    assert client.get("/live/A$$").status_code == 422


def test_stream_sends_snapshot_then_ticks(live_client):
    client, hub = live_client
    with client.stream("GET", "/live/AAPL/stream?max_events=2") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        events = read_events(r)
    names = [n for n, _ in events]
    assert names[0] == "snapshot" and names.count("tick") == 2 and "status" in names
    assert [d["price"] for n, d in events if n == "tick"] == [247.0, 246.5]
    assert hub.unsubscribed == ["AAPL"]


def test_stream_unknown_symbol_is_fatal(live_client):
    client, _ = live_client
    with client.stream("GET", "/live/ZZZZ/stream") as r:
        events = read_events(r)
    assert len(events) == 1
    name, data = events[0]
    assert name == "failure" and data["code"] == "UNKNOWN_SYMBOL" and data["fatal"] is True


def test_watchlist_stream_multiplexes_symbols(live_client):
    client, hub = live_client
    with client.stream("GET", "/watchlist/stream?symbols=AAPL,MSFT,ZZZZ,A$$&max_events=2") as r:
        events = read_events(r)
    quotes = {d["symbol"]: d for n, d in events if n == "quote"}
    failures = {d["symbol"]: d for n, d in events if n == "failure"}
    assert set(quotes) == {"AAPL", "MSFT"}
    assert quotes["AAPL"]["name"] == "Apple Inc. (mock)" and quotes["AAPL"]["spark"]
    assert failures["ZZZZ"]["code"] == "UNKNOWN_SYMBOL" and failures["ZZZZ"]["fatal"]
    assert failures["A$$"]["code"] == "INVALID_INPUT"
    assert sum(1 for n, _ in events if n == "tick") == 2
    assert sorted(hub.unsubscribed) == ["AAPL", "MSFT"]


def test_watchlist_rejects_empty_or_huge_lists(live_client):
    client, _ = live_client
    assert client.get("/watchlist/stream?symbols=").status_code == 422
    many = ",".join(f"S{i}" for i in range(61))
    assert client.get(f"/watchlist/stream?symbols={many}").status_code == 422


def test_forecast_endpoint(live_client):
    client, _ = live_client
    body = client.get("/forecast/RWLK?horizon_days=5").json()
    assert body["forecast"]["direction"] in ("up", "down", "no_clear_edge")
    assert body["forecast"]["backtest"]["models"][0]["predictions"] >= 20
    short = client.get("/forecast/AAPL").json()
    assert short["forecast"] is None and "daily bars" in short["reason"]
    assert client.get("/forecast/ZZZZ").status_code == 404
    assert client.get("/forecast/AAPL?horizon_days=99").status_code == 422
