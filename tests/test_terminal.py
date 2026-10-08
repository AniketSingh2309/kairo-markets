"""Terminal chart data: candles at 1m / 5m / 15m / 1h / 1D, served column-wise."""

from __future__ import annotations

import datetime as dt

import pytest

from api.market import BAR_CACHES
from tests.test_charts_logos import provider_for, yahoo_chart
from tests.test_platform_api import env  # noqa: F401 - shared fixture
from tools.errors import InvalidInputError
from tools.market_tools import get_bars

START = dt.datetime(2026, 10, 6, 3, 45, tzinfo=dt.timezone.utc)


@pytest.mark.parametrize("interval,yahoo_interval,yahoo_range", [
    ("1m", "1m", "7d"), ("5m", "5m", "60d"), ("15m", "15m", "60d"), ("1h", "60m", "2y"),
])
def test_intraday_intervals_ask_yahoo_for_the_longest_history(interval, yahoo_interval, yahoo_range):
    prov, seen = provider_for({yahoo_interval: yahoo_chart(yahoo_interval, [100, 101, None, 102], START, dt.timedelta(minutes=5))})
    bars = get_bars("TEST.NS", interval, provider=prov, timeout_s=5).data
    assert seen[0]["interval"] == yahoo_interval and seen[0]["range"] == yahoo_range
    assert bars.interval == interval and [b.close for b in bars.bars] == [100, 101, 102] and bars.previous_close == 99.0


def test_daily_uses_an_explicit_window_not_range_max():
    prov, seen = provider_for({"1d": yahoo_chart("1d", [50.0 + i for i in range(30)], START - dt.timedelta(days=40), dt.timedelta(days=1))})
    bars = get_bars("TEST.NS", "1D", provider=prov, timeout_s=5).data
    assert "range" not in seen[0] and seen[0]["period1"] == "0"     # range=max would quietly return monthly bars
    assert len(bars.bars) == 30 and bars.previous_close is None


def test_bad_interval_is_rejected():
    with pytest.raises(InvalidInputError):
        get_bars("TEST.NS", "2m", provider=provider_for({})[0], timeout_s=5)


def test_terminal_bars_endpoint(env):  # noqa: F811
    client, _, _ = env
    for cache in BAR_CACHES.values():
        cache._data.clear()
    body = client.get("/terminal/bars/AAPL?interval=1D").json()
    n = len(body["t"])
    assert n > 10 and all(len(body[k]) == n for k in "ohlcv") and body["interval"] == "1D"
    assert body["t"] == sorted(body["t"]) and all(isinstance(x, int) for x in body["t"])
    assert all(body["l"][i] <= body["c"][i] <= body["h"][i] for i in range(n))
    assert client.get("/terminal/bars/AAPL?interval=5m").status_code == 503   # mock data has no intraday bars
    assert client.get("/terminal/bars/AAPL?interval=2m").status_code == 422
    assert client.get("/terminal/bars/ZZZZ?interval=1D").status_code == 404


def test_previous_close_comes_from_daily_candles_when_yahoo_is_a_session_stale():
    # Yahoo's intraday meta said 22555.8 (two sessions back); the daily candles say 22776.1 for the day before.
    day = dt.timedelta(days=1)
    intraday = yahoo_chart("5m", [22600.0, 22603.05], START, dt.timedelta(minutes=5), prev=22555.8)
    daily = yahoo_chart("1d", [22555.75, 22776.1, 22603.05], START - 2 * day, day)
    prov, _ = provider_for({"5m": intraday, "1d": daily})
    assert get_bars("TEST.NS", "5m", provider=prov, timeout_s=5).data.previous_close == 22776.1
