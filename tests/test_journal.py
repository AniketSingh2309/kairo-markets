"""Trade journal: round trips from fills, R-multiples, statistics and insights."""

import asyncio
import datetime as dt

from core import journal as jr
from core import trading as tr

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def f(i, side, qty, price, hour=10, minute=0, day=8, sym="TCS.NS", product="INTRADAY", order=None):
    return {"id": i, "order_id": order or i, "at": dt.datetime(2026, 10, day, hour, minute, tzinfo=IST).isoformat(),
            "symbol": sym, "side": side, "qty": qty, "price": price, "product": product}


def test_round_trips_scale_in_out_and_flip():
    fills = [f(1, "BUY", 10, 100), f(2, "BUY", 10, 110, minute=5), f(3, "SELL", 5, 120, minute=10), f(4, "SELL", 15, 115, minute=20),  # long 20 @105
             f(5, "SELL", 10, 200, 11, sym="INFY.NS"), f(6, "BUY", 15, 190, 11, 30, sym="INFY.NS"),                                        # short 10, flip to long 5
             f(7, "SELL", 5, 195, 12, sym="INFY.NS")]
    t = {x["key"]: x for x in jr.round_trips(fills)}
    long = t["TCS.NS|INTRADAY|1"]
    assert long["side"] == "LONG" and long["entry"] == 105 and long["exit"] == 116.25 and long["pnl"] == 225 and long["qty"] == 20
    assert long["hold_minutes"] == 20 and long["status"] == "closed"
    short = t["INFY.NS|INTRADAY|5"]
    assert short["side"] == "SHORT" and short["pnl"] == 100
    flipped = t["INFY.NS|INTRADAY|6b"]
    assert flipped["side"] == "LONG" and flipped["entry"] == 190 and flipped["pnl"] == 25


def test_open_trade_and_r_multiple():
    fills = [f(1, "BUY", 10, 100, order=50), f(2, "SELL", 10, 104), f(3, "BUY", 5, 200, sym="X.NS")]
    trades = jr.round_trips(fills, init_stops={50: 98})
    closed = next(x for x in trades if x["symbol"] == "TCS.NS")
    assert closed["risk"] == 20 and closed["r"] == 2
    assert next(x for x in trades if x["symbol"] == "X.NS")["status"] == "open"


def test_stats_and_insights():
    fills, i = [], 0
    for k, (entry, exit_, hold) in enumerate([(100, 103, 10), (100, 95, 60), (100, 96, 45), (100, 102, 5), (100, 94, 90)]):
        i += 2
        fills += [f(i, "BUY", 10, entry, 10 + k, 0), f(i + 1, "SELL", 10, exit_, 10 + k + hold // 60, hold % 60)]
    trades = jr.round_trips(fills)
    notes = {trades[0]["key"]: {"setup": "Breakout", "mistakes": ["Moved my stop"]}}
    s = jr.stats(trades, notes)
    assert s["trades"] == 5 and s["wins"] == 2 and s["win_rate"] == 40 and s["total"] == -100
    assert s["avg_win"] == 25 and s["avg_loss"] == -50 and s["profit_factor"] == round(50 / 150, 2)
    assert s["max_drawdown"] <= -100 and len(s["equity"]) == 5
    text = " ".join(s["insights"])
    assert "average loss" in text and "no stop" in text and "hold losing trades" in text
    assert jr.stats([])["trades"] == 0


def test_journal_api_with_paper_trades():
    from fastapi.testclient import TestClient
    from api import deps
    from api.main import app
    from core.store import Store
    store = Store(":memory:")
    for i, (side, px) in enumerate([("BUY", 100.0), ("SELL", 110.0)], 1):
        oid = store.add_paper_order({"symbol": "TCS.NS", "side": side, "qty": 5, "order_type": "MARKET", "product": "DELIVERY", "status": "open",
                                     "session": "2026-10-08", "created_at": "2026-10-08T05:00:00+00:00"})
        store.fill_paper_order(oid, ("open",), {"at": f"2026-10-08T0{4 + i}:00:00+00:00", "symbol": "TCS.NS", "side": side, "qty": 5, "price": px, "product": "DELIVERY"})
    app.dependency_overrides[deps.get_store] = lambda: store
    try:
        c = TestClient(app)
        j = c.get("/journal").json()
        assert j["stats"]["total"] == 50 and j["trades"][0]["notes"] is None
        key = j["trades"][0]["key"]
        assert c.put(f"/journal/{key}", json={"setup": "Breakout", "mistakes": ["Chased the move"], "note": "ok", "rating": 4}).status_code == 200
        j = c.get("/journal").json()
        assert j["trades"][0]["notes"]["setup"] == "Breakout" and j["stats"]["by_setup"][0]["name"] == "Breakout"
        assert c.put("/journal/NOPE|X|1", json={}).status_code == 404
    finally:
        app.dependency_overrides.clear()
