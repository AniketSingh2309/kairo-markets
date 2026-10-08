"""Phase 1 Explore: daily snapshots, screens with track records, movers/screens endpoints."""

from __future__ import annotations

import datetime as dt
import time

import pytest

from core.signals import SCREENS, run_screens, series_of
from tests.test_forecast import make_bars, random_walk
from tests.test_platform_api import env  # noqa: F401 - shared fixture
from tools.market_tools import get_daily_snapshot


def test_daily_snapshot_from_mock(mock_provider):
    d = get_daily_snapshot("AAPL", provider=mock_provider, timeout_s=2).data
    assert d.price == 246.1 and d.previous_close == 245.25
    assert d.change_pct == pytest.approx(0.3466, abs=1e-3)
    assert d.volume_ratio == pytest.approx(50870000 / d.avg_volume, rel=1e-3)
    assert d.fifty_two_week_high == 246.8 and len(d.closes) == 21
    assert d.session_date == dt.date(2026, 9, 29)


def _bars_with_breakout():
    closes = [100.0] * 60 + [100 + i * 0.01 for i in range(5)] + [110.0, 111, 112]
    return make_bars(closes)


def test_breakout_is_an_event_not_a_state():
    bars = _bars_with_breakout()
    s = series_of(bars)
    breakout = next(sc for sc in SCREENS if sc.key == "breakout")
    fired = [i for i in range(len(bars)) if breakout.test(s, i)]
    assert len(fired) == 1 and bars[fired[0]].close == 110.0  # only the day it broke out, not every day after


def test_recent_trigger_is_listed_as_a_match():
    res = {r.key: r for r in run_screens({"X": ("X Ltd", _bars_with_breakout())}, horizon=5)}
    m = res["breakout"].matches
    assert [x.symbol for x in m] == ["X"] and m[0].sessions_ago == 2


def test_track_record_needs_enough_events_and_compares_to_base_rate():
    hist = {f"S{i}": (None, make_bars(random_walk(500, i))) for i in range(12)}
    res = run_screens(hist, horizon=10)
    assert {r.key for r in res} == {s.key for s in SCREENS}
    for r in res:
        t = r.track
        if t.events < 15:
            assert t.verdict == "too_few_events"
        else:
            assert 0 <= t.hit_rate <= 1 and t.base_rate is not None and t.ci_low <= t.hit_rate <= t.ci_high
    # Random walks: no screen should look reliably better than the base rate.
    assert not any(r.track.verdict == "beats_base_rate" and r.track.ci_low > r.track.base_rate + 0.08 for r in res)


def test_screens_never_use_future_bars():
    bars = make_bars(random_walk(300, 3))
    full, cut = series_of(bars), series_of(bars[:200])
    for sc in SCREENS:
        assert [sc.test(full, i) for i in range(200)] == [sc.test(cut, i) for i in range(200)], sc.key


def _poll(client, url, timeout=15):
    deadline = time.time() + timeout
    while True:
        body = client.get(url).json()
        if body["status"] == "ready" or time.time() > deadline:
            return body
        time.sleep(0.1)


def test_movers_endpoint(env, monkeypatch):  # noqa: F811
    client, _, _ = env
    monkeypatch.setitem(__import__("core.screener", fromlist=["UNIVERSES"]).UNIVERSES, "mock",
                        ("Mock", ["AAPL", "MSFT", "TSLA", "ZZZZ"]))
    body = _poll(client, "/explore/movers?universe=mock")
    rows = {r["symbol"]: r for r in body["result"]}
    assert set(rows) == {"AAPL", "MSFT", "TSLA"} and body["errors"] == {"ZZZZ": "UNKNOWN_SYMBOL"}
    assert rows["AAPL"]["sector"] == "Technology" and rows["AAPL"]["closes"]
    assert rows["AAPL"]["from_52w_high"] <= 0
    assert client.get("/explore/movers?universe=nope").status_code == 404


def test_screens_endpoint(env, monkeypatch):  # noqa: F811
    client, _, _ = env
    monkeypatch.setitem(__import__("core.screener", fromlist=["UNIVERSES"]).UNIVERSES, "mock2", ("Mock", ["RWLK", "AAPL"]))
    body = _poll(client, "/explore/screens?universe=mock2&horizon=10")
    assert body["status"] == "ready" and len(body["result"]) == len(SCREENS)
    assert all("track" in r and r["track"]["horizon"] == 10 for r in body["result"])
