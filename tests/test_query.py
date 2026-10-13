"""Screener query language and the lazy fundamentals fetch."""

import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest

from core.query import QueryError, parse, technical_fields


def test_parse_and_evaluate():
    q = parse("roe > 15 AND (pe < 25 OR pb < 3) AND NOT asm")
    assert q.fields == {"roe", "pe", "pb", "asm"} and q.groups == {"fundamental", "india"}
    assert q.evaluate({"roe": 20, "pe": 30, "pb": 2, "asm": False}) is True
    assert q.evaluate({"roe": 20, "pe": 30, "pb": 4, "asm": False}) is False
    assert parse("close > sma200 * 1.05").evaluate({"close": 106, "sma200": 100}) is True
    assert parse("chg_1d >= -2 && rsi < 30").evaluate({"chg_1d": -1.5, "rsi": 25}) is True
    assert parse("golden_cross").evaluate({"golden_cross": True}) is True
    assert parse("vol_x = 2").evaluate({"vol_x": 2.0}) is True


def test_three_valued_logic():
    q = parse("rsi < 30 AND roe > 15")
    assert q.evaluate({"rsi": 50}) is False          # decided without fundamentals
    assert q.evaluate({"rsi": 20}) is None           # needs ROE
    assert parse("rsi < 30 OR roe > 15").evaluate({"rsi": 20}) is True
    assert parse("NOT roe > 15").evaluate({}) is None
    assert parse("pe / pb > 2").evaluate({"pe": 10, "pb": 0}) is None   # division by zero is unknown


@pytest.mark.parametrize("bad, msg", [("", "Write a condition"), ("roe >", "ends too early"), ("roee > 1", "Did you mean roe"),
                                      ("roe > 1 pe", "Unexpected"), ("(roe > 1", "Expected"), ("roe ? 1", "don't understand"),
                                      ("__import__", "Unknown field")])
def test_errors(bad, msg):
    with pytest.raises(QueryError, match=msg):
        parse(bad)


def bars(n=260, start=100.0, step=0.5):
    out, p = [], start
    for i in range(n):
        o, p = p, p + step
        out.append(SimpleNamespace(date=dt.date(2025, 1, 1) + dt.timedelta(days=i), open=o, high=p + 1, low=o - 1, close=p, volume=1000 if i < n - 1 else 3000))
    return out


def test_technical_fields():
    t = technical_fields(bars())
    assert t["close"] == 230 and t["sma50"] < t["close"] and t["golden_cross"] is True and t["vol_x"] == 3
    assert t["rsi"] == 100 and t["from_52w_high"] < 0 and t["chg_1d"] > 0


def test_run_query_fetches_fundamentals_only_when_undecided(monkeypatch):
    import api.scans as sc
    from tools.models import Fundamentals
    hist = {"UP.NS": bars(step=0.5), "DOWN.NS": bars(step=-0.2, start=200)}
    fetched = []

    async def history(sym, period, provider):
        return SimpleNamespace(name=sym, currency="INR", bars=hist[sym])

    async def fundamentals_of(sym, provider, settings, statements=True):
        fetched.append(sym)
        return SimpleNamespace(data=Fundamentals(symbol=sym, roe=0.22, pe=18.0, market_cap=5e11))

    monkeypatch.setattr(sc.deps, "history", history)
    monkeypatch.setattr(sc, "fundamentals_of", fundamentals_of)
    rows = asyncio.run(sc.run_query(parse("close > sma200 AND roe > 15"), ["UP.NS", "DOWN.NS"], None, None))
    assert [r["symbol"] for r in rows] == ["UP.NS"] and fetched == ["UP.NS"]        # DOWN was ruled out by price
    assert rows[0]["roe"] == 22 and set(rows[0]) >= {"symbol", "close", "chg_1d", "sma200", "roe"}


def test_scan_alert_helpers():
    import api.scans as sc
    assert sc.new_matches(["A", "B"], ["B", "C", "D"]) == ["C", "D"]
    ist = sc.IST
    mon = dt.datetime(2026, 10, 12, 11, 0, tzinfo=ist)
    assert sc._due(mon, None) and not sc._due(mon, mon - dt.timedelta(minutes=5)) and sc._due(mon, mon - dt.timedelta(minutes=16))
    assert not sc._due(dt.datetime(2026, 10, 11, 11, 0, tzinfo=ist), None)                    # Sunday
    after = dt.datetime(2026, 10, 12, 15, 50, tzinfo=ist)
    assert sc._due(after, dt.datetime(2026, 10, 12, 15, 25, tzinfo=ist)) and not sc._due(after, after - dt.timedelta(minutes=4))


def test_alert_scans_announce_only_new_matches(monkeypatch):
    import api.scans as sc
    from core.store import Store
    store = Store(":memory:")
    store.save_scan("Breakouts", "nifty50", "vol_x > 2", alert=True)
    store.save_scan("Quiet", "nifty50", "rsi < 30", alert=False)
    matches = iter([["A.NS", "B.NS"], ["B.NS", "C.NS", "D.NS"], ["C.NS"]])

    async def run_query(q, symbols, provider, india, job=None):
        return [{"symbol": s} for s in next(matches)]
    monkeypatch.setattr(sc, "run_query", run_query)
    monkeypatch.setattr(sc, "load_universe", lambda key, registry: SimpleNamespace(key=key, symbols=["A.NS", "B.NS", "C.NS", "D.NS"]))
    events = []
    engine = SimpleNamespace(_publish=events.append)
    assert asyncio.run(sc.run_alert_scans(store, None, None, None, engine)) == 0          # first run: baseline only
    assert asyncio.run(sc.run_alert_scans(store, None, None, None, engine)) == 1
    assert events[0]["kind"] == "scan" and events[0]["scan"]["symbols"] == ["C.NS", "D.NS"] and "2 new matches: C, D" in events[0]["message"]
    assert asyncio.run(sc.run_alert_scans(store, None, None, None, engine)) == 0          # C was already matching
    assert next(s for s in store.scans() if s["name"] == "Quiet")["last_run"] is None          # the non-alert scan never ran
