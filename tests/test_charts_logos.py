"""Chart ranges (1D ... ALL), the chart API, and the logo fetcher (offline: fake transports, mock provider)."""

from __future__ import annotations

import asyncio
import datetime as dt

import httpx
import pytest

from api.market import CHARTS_DAILY, CHARTS_INTRADAY
from tests.test_platform_api import env  # noqa: F401 - shared fixture
from tools.market_tools import get_chart
from tools.providers.logos import LogoStore, candidates
from tools.providers.yahoo_provider import YahooFinanceProvider

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 300


def yahoo_chart(interval: str, closes: list[float | None], start: dt.datetime, step: dt.timedelta,
                granularity: str | None = None, prev: float | None = 99.0):
    ts = [int((start + step * i).timestamp()) for i in range(len(closes))]
    meta = {"symbol": "TEST.NS", "currency": "INR", "longName": "Test Ltd", "chartPreviousClose": prev,
            "regularMarketTime": ts[-1], "dataGranularity": granularity or interval}
    return {"chart": {"result": [{"meta": meta, "timestamp": ts, "indicators": {"quote": [{
        "open": [c and c - 1 for c in closes], "high": [c and c + 2 for c in closes],
        "low": [c and c - 2 for c in closes], "close": closes, "volume": [1000] * len(closes)}]}}], "error": None}}


def provider_for(payloads: dict[str, dict]):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        if params["interval"] not in payloads:
            return httpx.Response(404)  # e.g. the daily lookup for the previous close: not part of this test
        return httpx.Response(200, json=payloads[params["interval"]])
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://query1.finance.yahoo.com")
    return YahooFinanceProvider(client=client), seen


def test_intraday_chart_skips_empty_buckets_and_keeps_previous_close():
    start = dt.datetime(2026, 10, 6, 3, 45, tzinfo=dt.timezone.utc)
    prov, seen = provider_for({"5m": yahoo_chart("5m", [100, None, 101, 102], start, dt.timedelta(minutes=5))})
    c = get_chart("TEST.NS", "1D", provider=prov, timeout_s=5).data
    assert seen[0]["range"] == "1d" and seen[0]["interval"] == "5m"
    assert c.intraday and c.interval == "5m" and [b.close for b in c.bars] == [100, 101, 102]
    assert c.previous_close == 99.0 and c.visible_from == c.bars[0].t
    assert all(b.low <= min(b.open, b.close) and b.high >= max(b.open, b.close) for b in c.bars)


def test_daily_range_fetches_warmup_and_sets_visible_window():
    start = dt.datetime(2024, 10, 1, tzinfo=dt.timezone.utc)
    closes = [100 + i * 0.1 for i in range(600)]
    prov, seen = provider_for({"1d": yahoo_chart("1d", closes, start, dt.timedelta(days=1))})
    c = get_chart("TEST.NS", "3M", provider=prov, timeout_s=5).data
    assert seen[0]["range"] == "1y"                                   # extra history so SMAs are warm
    visible = [b for b in c.bars if b.t >= c.visible_from]
    assert 90 <= len(visible) <= 93
    before = [b for b in c.bars if b.t < c.visible_from]
    assert c.previous_close == before[-1].close                      # change is measured from the close before the range


def test_all_range_uses_what_yahoo_actually_sent():
    start = dt.datetime(2021, 7, 18, tzinfo=dt.timezone.utc)
    weekly = yahoo_chart("1mo", [50 + i for i in range(270)], start, dt.timedelta(weeks=1), granularity="1wk")
    prov, _ = provider_for({"1mo": weekly})
    c = get_chart("TEST.NS", "ALL", provider=prov, timeout_s=5).data
    assert c.interval == "1wk" and not c.intraday and c.previous_close is None


def test_all_range_falls_back_to_finer_bars_for_young_listings():
    start = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)
    prov, seen = provider_for({
        "1mo": yahoo_chart("1mo", [10.0 + i for i in range(20)], start, dt.timedelta(days=30)),
        "1wk": yahoo_chart("1wk", [10.0 + i for i in range(80)], start, dt.timedelta(weeks=1)),
    })
    c = get_chart("TEST.NS", "ALL", provider=prov, timeout_s=5).data
    assert [p["interval"] for p in seen] == ["1mo", "1wk"] and len(c.bars) == 80


def test_chart_endpoint_with_overlays(env):  # noqa: F811
    client, _, _ = env
    CHARTS_DAILY._data.clear(); CHARTS_INTRADAY._data.clear()
    body = client.get("/chart/AAPL?range=ALL").json()
    assert body["range"] == "ALL" and body["interval"] == "1d" and body["bars"]
    bar = body["bars"][-1]
    assert {"t", "o", "h", "l", "c", "sma20", "sma50", "sma200", "bb_upper", "bb_lower"} <= set(bar)
    assert body["high"] >= max(b["c"] for b in body["bars"]) and body["low"] <= min(b["c"] for b in body["bars"])
    first = body["bars"][0]
    assert body["change"] == pytest.approx(bar["c"] - first["o"], abs=1e-6)  # no earlier close: measured from the first open
    assert client.get("/chart/AAPL?range=1D").status_code == 503      # mock data has no intraday bars
    assert client.get("/chart/AAPL?range=2D").status_code == 422
    assert client.get("/chart/ZZZZ?range=1Y").status_code == 404


# --- logos -----------------------------------------------------------------------------------------------


def test_logo_candidates():
    nse = candidates("M&M.NS", "INE101A01026")
    assert nse[0] == "https://eodhd.com/img/logos/NSE/M%26M.png" and nse[-1].endswith("/isin/INE101A01026?format=png")
    assert candidates("AAPL")[0] == "https://eodhd.com/img/logos/US/AAPL.png"
    assert candidates("BRK-B")[0].endswith("/US/BRK-B.png")                 # class share, not a coin
    assert candidates("HYPE32196-USD")[0] == "https://assets.parqet.com/logos/crypto/HYPE?format=png"
    assert candidates("^NSEI") == [] and candidates("USDINR=X") == []


def test_logo_store_falls_back_caches_and_remembers_misses(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "eodhd" in str(request.url):
            return httpx.Response(404)
        if "image-stock/INFY.NS" in str(request.url):
            return httpx.Response(200, content=PNG)
        if "image-stock" in str(request.url):
            return httpx.Response(200, content=b"<html>not an image</html>")  # error pages are not logos
        return httpx.Response(404)

    async def run():
        store = LogoStore(tmp_path, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        hit = await store.get("INFY.NS")
        miss = await store.get("ZZZQ.NS")
        n = len(calls)
        again = LogoStore(tmp_path, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        assert (await again.get("INFY.NS")).content == PNG and await again.get("ZZZQ.NS") is None
        return hit, miss, n

    hit, miss, n = asyncio.run(run())
    assert hit and hit.content_type == "image/png" and miss is None
    assert len(calls) == n                                    # second store answered from disk, both hit and miss


def test_logo_endpoint(env, monkeypatch):  # noqa: F811
    from api import deps
    from api.main import app

    class FakeLogos:
        async def get(self, symbol, isin=None):
            from tools.providers.logos import Logo
            return Logo(PNG, "image/png") if symbol == "AAPL" else None

    app.dependency_overrides[deps.get_logo_store] = lambda: FakeLogos()
    client, _, _ = env
    r = client.get("/logo/AAPL")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and "max-age" in r.headers["cache-control"]
    assert client.get("/logo/MSFT").status_code == 404
    assert client.get("/logo/MF122639").status_code == 404
    assert client.get("/logo/not a symbol").status_code == 404


# --- speed & resilience ------------------------------------------------------------------------------------


def test_stalled_yahoo_request_is_retried_on_the_other_host_quickly():
    start = dt.datetime(2026, 10, 6, 3, 45, tzinfo=dt.timezone.utc)
    good = yahoo_chart("5m", [100, 101], start, dt.timedelta(minutes=5))
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "query1.finance.yahoo.com":
            raise httpx.ReadTimeout("stalled", request=request)
        return httpx.Response(200, json=good)
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://query1.finance.yahoo.com")
    prov = YahooFinanceProvider(client=client, timeout_s=8)
    assert prov._first_timeout.read <= 3.0 and prov._first_timeout.read + prov._retry_timeout.read < 8
    c = get_chart("TEST.NS", "1D", provider=prov, timeout_s=8).data
    assert hosts == ["query1.finance.yahoo.com", "query2.finance.yahoo.com"] and len(c.bars) == 2


def test_chart_endpoint_serves_last_good_chart_when_a_refresh_fails(env, monkeypatch):  # noqa: F811
    import api.market as market
    from tools.errors import ToolTimeoutError

    client, _, _ = env
    CHARTS_DAILY._data.clear(); CHARTS_INTRADAY._data.clear(); market._LAST_GOOD.clear()
    ok = client.get("/chart/AAPL?range=1Y").json()
    assert ok["stale"] is False
    CHARTS_DAILY._data.clear()

    def stalled(*a, **k):
        raise ToolTimeoutError("get_chart did not respond within 8.00s", tool="get_chart", symbol="AAPL")
    monkeypatch.setattr(market, "get_chart", stalled)
    again = client.get("/chart/AAPL?range=1Y")
    assert again.status_code == 200 and again.json()["stale"] is True and again.json()["bars"] == ok["bars"]
    assert client.get("/chart/MSFT?range=1Y").status_code == 503    # nothing to fall back to
