"""Paper trading engine, the trading API (paper mode), and the Upstox connector (simulated HTTP)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from core import trading as tr
from core.store import Store
from tools.providers.upstox import UpstoxBroker, UpstoxError, app_symbol, instrument_token

IST = tr.IST
OPT = "OPT:OPTIDXNIFTY13-10-2026CE22600.00"


def at(h, m, day=dt.date(2026, 10, 7)):  # a Wednesday
    return dt.datetime.combine(day, dt.time(h, m), tzinfo=IST).astimezone(dt.timezone.utc)


def fill(day_offset, symbol, side, qty, price, product="DELIVERY", hour=10):
    return {"at": at(hour, 0, dt.date(2026, 10, 7) + dt.timedelta(days=day_offset)), "symbol": symbol, "side": side,
            "qty": qty, "price": price, "product": product}


# --- orders -----------------------------------------------------------------------------------------


def test_order_validation():
    assert tr.OrderIn(symbol="reliance.ns", side="BUY", qty=1).symbol == "RELIANCE.NS"
    assert tr.OrderIn(symbol=OPT.lower(), side="BUY", qty=75, product="INTRADAY").symbol == OPT
    for bad in [dict(symbol="AAPL", side="BUY", qty=1), dict(symbol="^NSEI", side="BUY", qty=1),
                dict(symbol="OPT:junk", side="BUY", qty=1), dict(symbol="TCS.NS", side="BUY", qty=0),
                dict(symbol="TCS.NS", side="BUY", qty=1, order_type="LIMIT"),
                dict(symbol="TCS.NS", side="BUY", qty=1, order_type="SL-M"),
                dict(symbol="TCS.NS", side="BUY", qty=1, order_type="SL", price=99, trigger_price=100),
                dict(symbol="TCS.NS", side="SELL", qty=1, order_type="SL", price=101, trigger_price=100)]:
        with pytest.raises(ValueError):
            tr.OrderIn(**bad)
    o = tr.OrderIn(symbol="TCS.NS", side="BUY", qty=1, order_type="MARKET", price=5, trigger_price=4)
    assert o.price is None and o.trigger_price is None                     # market orders ignore stray prices


def test_matching_rules():
    o = lambda **k: {"side": "BUY", "order_type": "MARKET", "status": "open", "price": None, "trigger_price": None, **k}  # noqa: E731
    assert tr.match(o(), 100) == ("complete", 100)
    assert tr.match(o(order_type="LIMIT", price=99), 100) == ("open", None)
    assert tr.match(o(order_type="LIMIT", price=99), 98.5) == ("complete", 98.5)          # fills at the better price
    assert tr.match(o(side="SELL", order_type="LIMIT", price=101), 101.2) == ("complete", 101.2)
    assert tr.match(o(order_type="SL-M", trigger_price=105), 104) == ("open", None)
    assert tr.match(o(order_type="SL-M", trigger_price=105), 105.5) == ("complete", 105.5)
    assert tr.match(o(side="SELL", order_type="SL-M", trigger_price=95), 94) == ("complete", 94)
    assert tr.match(o(order_type="SL", trigger_price=105, price=106), 107) == ("triggered", None)  # triggered, above limit
    assert tr.match(o(order_type="SL", trigger_price=105, price=106, status="triggered"), 105.8) == ("complete", 105.8)


def test_sessions_and_hours():
    assert tr.order_session(at(10, 0)) == dt.date(2026, 10, 7)
    assert tr.order_session(at(16, 0)) == dt.date(2026, 10, 8)                           # after the close -> next day
    assert tr.order_session(at(16, 0, dt.date(2026, 10, 9))) == dt.date(2026, 10, 12)   # Friday evening -> Monday
    assert tr.market_open(at(9, 15)) and not tr.market_open(at(15, 30)) and not tr.market_open(at(9, 14))
    assert not tr.market_open(at(11, 0, dt.date(2026, 10, 10)))                          # Saturday


# --- book --------------------------------------------------------------------------------------------


def test_delivery_cash_and_holdings_carry():
    today = dt.date(2026, 10, 8)
    book = tr.replay([fill(0, "TCS.NS", "BUY", 10, 100), fill(0, "TCS.NS", "BUY", 10, 110),
                      fill(1, "TCS.NS", "SELL", 5, 120)], 100_000, today)
    ln = book.lines[("TCS.NS", "DELIVERY")]
    assert ln.qty == 15 and ln.avg == 105 and ln.realised == 75 and ln.realised_today == 75
    assert book.cash == 100_000 - 2100 + 600
    h = tr.holding_rows(book, {"TCS.NS": 130}, {"TCS.NS": 125})
    assert h == [{"symbol": "TCS.NS", "qty": 15, "avg": 105, "ltp": 130, "invested": 1575, "value": 1950, "pnl": 375, "day_change": 75}]
    pos = tr.position_rows(book, {"TCS.NS": 130})
    assert pos[0]["today_qty"] == -5 and pos[0]["realised"] == 75 and pos[0]["sell_qty"] == 5


def test_intraday_margin_short_and_flip():
    today = dt.date(2026, 10, 7)
    book = tr.replay([fill(0, "SBIN.NS", "SELL", 10, 200, "INTRADAY"), fill(0, "SBIN.NS", "BUY", 4, 190, "INTRADAY", 11)], 50_000, today)
    ln = book.lines[("SBIN.NS", "INTRADAY")]
    assert ln.qty == -6 and ln.realised == 40 and book.cash == 50_040              # short covered 4 at a profit
    assert book.margin_used() == pytest.approx(6 * 200 * 0.2)
    book2 = tr.replay([fill(0, "SBIN.NS", "BUY", 10, 100, "INTRADAY"), fill(0, "SBIN.NS", "SELL", 15, 110, "INTRADAY", 11)], 0, today)
    ln2 = book2.lines[("SBIN.NS", "INTRADAY")]
    assert ln2.qty == -5 and ln2.avg == 110 and ln2.realised == 100                # flipped short at the fill price
    assert [x.symbol for x in tr.to_square_off(book2)] == ["SBIN.NS"]


def test_options_pay_premium_and_writing_needs_margin():
    book = tr.replay([fill(0, OPT, "BUY", 75, 120, "INTRADAY")], 100_000, dt.date(2026, 10, 7))
    assert book.cash == 100_000 - 9000 and book.margin_used() == 0
    assert tr.check_order(tr.OrderIn(symbol=OPT, side="SELL", qty=75, product="INTRADAY"), 125, book, [], {}) is None  # closing: free
    write = tr.OrderIn(symbol=OPT, side="SELL", qty=150, product="INTRADAY")                  # 75 to close + 75 new short
    assert "Not enough funds" in tr.check_order(write, 125, book, [], {})                       # 15% x 22600 x 75 = 254,250
    rich = tr.replay([fill(0, OPT, "BUY", 75, 120, "INTRADAY")], 1_000_000, dt.date(2026, 10, 7))
    assert tr.check_order(write, 125, rich, [], {}) is None


def test_short_option_book_and_margin():
    book = tr.replay([fill(0, OPT, "SELL", 65, 100, "INTRADAY")], 1_000_000, dt.date(2026, 10, 7))
    assert book.cash == 1_000_000 + 6500                                                        # premium received
    assert book.margin_used() == pytest.approx(0.15 * 22600 * 65)
    cover = {"symbol": OPT, "side": "BUY", "qty": 65, "price": 80, "trigger_price": None, "product": "INTRADAY", "status": "open"}
    assert tr.reserve_for(cover, 80, book) == 0                                                 # buying back frees margin
    rows = tr.position_rows(book, {OPT: 80})
    assert rows[0]["qty"] == -65 and rows[0]["pnl"] == 1300                                    # short gains as premium falls
    later = tr.replay([fill(0, OPT, "BUY", 65, 100, "DELIVERY")], 1_000_000, dt.date(2026, 10, 9))
    assert tr.position_rows(later, {OPT: 110})[0]["qty"] == 65                                  # carried options stay in positions
    assert tr.holding_rows(later, {OPT: 110}, {}) == []                                        # ...not holdings


def test_funds_checks():
    book = tr.replay([], 10_000, dt.date(2026, 10, 7))
    big = tr.OrderIn(symbol="TCS.NS", side="BUY", qty=10, product="DELIVERY")
    assert "Not enough funds" in tr.check_order(big, 2000, book, [], {})
    assert tr.check_order(tr.OrderIn(symbol="TCS.NS", side="BUY", qty=10, product="INTRADAY"), 2000, book, [], {}) is None  # 5x
    assert "Short selling is intraday only" in tr.check_order(tr.OrderIn(symbol="TCS.NS", side="SELL", qty=1), 2000, book, [], {})
    pending = [{"symbol": "TCS.NS", "side": "BUY", "qty": 4, "price": 2000, "trigger_price": None, "product": "DELIVERY", "status": "open"}]
    assert tr.funds(book, pending, {})["available"] == 2000                      # open buy orders hold back funds
    assert "No live price" in tr.check_order(tr.OrderIn(symbol="TCS.NS", side="BUY", qty=1), None, book, [], {})


# --- paper trader (engine + store, with a controllable clock and prices) ------------------------------------


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def trader():
    from api.trading import PaperTrader
    clock, prices = Clock(at(10, 0)), {"TCS.NS": 2000.0, OPT: 120.0}
    t = PaperTrader(Store(":memory:"), provider=None, nse=None, now=clock)

    async def ltp(sym):
        return prices.get(sym)
    t.ltp = ltp
    t.reset(100_000)
    return t, clock, prices


def run(coro):
    return asyncio.run(coro)


def test_paper_market_and_limit_orders(trader):
    t, clock, prices = trader
    o = run(t.place(tr.OrderIn(symbol="TCS.NS", side="BUY", qty=10)))
    assert o["status"] == "complete" and o["fill_price"] == 2000
    lim = run(t.place(tr.OrderIn(symbol="TCS.NS", side="BUY", qty=5, order_type="LIMIT", price=1950)))
    assert lim["status"] == "open"
    prices["TCS.NS"] = 1940.0
    run(t.tick())
    assert t._order(lim["id"])["status"] == "complete" and t._order(lim["id"])["fill_price"] == 1940
    f = run(t.funds())
    assert f["cash"] == 100_000 - 20_000 - 9700
    rej = run(t.place(tr.OrderIn(symbol="TCS.NS", side="BUY", qty=1000)))
    assert rej["status"] == "rejected" and "Not enough funds" in rej["message"]


def test_paper_orders_outside_hours_wait_and_expire(trader):
    t, clock, prices = trader
    clock.t = at(18, 0)                                                            # evening: queued for tomorrow
    o = run(t.place(tr.OrderIn(symbol="TCS.NS", side="BUY", qty=1)))
    assert o["status"] == "open"
    clock.t = at(9, 20, dt.date(2026, 10, 8)); run(t.tick())
    assert t._order(o["id"])["status"] == "complete"                              # filled at the next open
    lim = run(t.place(tr.OrderIn(symbol="TCS.NS", side="BUY", qty=1, order_type="LIMIT", price=1)))
    clock.t = at(15, 35, dt.date(2026, 10, 8)); run(t.tick())
    assert t._order(lim["id"])["status"] == "cancelled" and "Expired" in t._order(lim["id"])["message"]
    assert run(t.cancel(o["id"])) is False                                         # filled orders can't be cancelled


def test_paper_intraday_square_off(trader):
    t, clock, prices = trader
    run(t.place(tr.OrderIn(symbol="TCS.NS", side="SELL", qty=10, product="INTRADAY")))   # short 10 @ 2000
    prices["TCS.NS"] = 1990.0
    clock.t = at(15, 21); run(t.tick())
    assert not tr.to_square_off(t.book())
    last = t.orders()[0]
    assert last["message"] == "Auto square-off at 3:20 pm" and last["side"] == "BUY" and last["fill_price"] == 1990
    assert run(t.funds())["cash"] == 100_000 + 100


def test_paper_exit_and_reset(trader):
    t, clock, prices = trader
    run(t.place(tr.OrderIn(symbol=OPT, side="BUY", qty=75, product="INTRADAY")))
    prices[OPT] = 130.0
    out = run(t.exit(OPT, "INTRADAY"))
    assert out["status"] == "complete" and out["side"] == "SELL"
    assert run(t.positions())[0]["realised"] == 750 and run(t.positions())[0]["label"] == "NIFTY 13 Oct 22600 CE"
    t.reset(500_000)
    assert t.orders() == [] and run(t.funds())["cash"] == 500_000


# --- API (paper mode) --------------------------------------------------------------------------------


def test_trading_api_paper_mode(trader, monkeypatch):
    from api import deps
    from api.main import app
    from api.trading import get_paper, get_upstox
    t, clock, prices = trader
    t.ensure_running = lambda: None
    app.dependency_overrides.update({get_paper: lambda: t, get_upstox: lambda: None, deps.get_store: lambda: t.store})
    try:
        c = TestClient(app)
        s = c.get("/trading/status").json()
        assert s["mode"] == "paper" and s["upstox"] == {"configured": False, "connected": False, "user": None, "expires_at": None}
        r = c.post("/trading/orders", json={"symbol": "TCS.NS", "side": "BUY", "qty": 2}).json()
        assert r["mode"] == "paper" and r["order"]["status"] == "complete"
        assert c.post("/trading/orders", json={"symbol": "AAPL", "side": "BUY", "qty": 1}).status_code == 422
        assert c.post("/trading/mode", json={"mode": "upstox"}).status_code == 409    # not connected
        assert c.get("/trading/positions").json()["items"][0]["qty"] == 2
        assert c.get("/trading/funds").json()["cash"] == 100_000 - 4000
        assert len(c.get("/trading/orders").json()["items"]) == 1
        assert c.delete("/trading/orders/999").status_code == 409
        assert c.post("/trading/exit", json={"symbol": "TCS.NS", "product": "DELIVERY"}).json()["order"]["side"] == "SELL"
        assert c.get("/broker/upstox/login", follow_redirects=False).status_code == 409   # no keys configured
    finally:
        app.dependency_overrides.clear()


# --- Upstox connector -----------------------------------------------------------------------------------


def upstox(handler):
    return UpstoxBroker("KEY", "SECRET", "http://127.0.0.1:8000/broker/upstox/callback", token="TOK",
                        client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_upstox_login_url_and_token_exchange():
    seen = {}

    def handler(request):
        seen["url"], seen["body"], seen["auth"] = str(request.url), request.content.decode(), request.headers.get("authorization")
        return httpx.Response(200, json={"access_token": "NEW", "user_id": "AB1234", "user_name": "Test User"})
    b = upstox(handler)
    url = b.login_url("xyz")
    assert url.startswith("https://api.upstox.com/v2/login/authorization/dialog?") and "client_id=KEY" in url and "state=xyz" in url
    info = b.exchange("CODE1")
    assert seen["url"] == "https://api.upstox.com/v2/login/authorization/token" and seen["auth"] is None
    assert "grant_type=authorization_code" in seen["body"] and "code=CODE1" in seen["body"] and "client_secret=SECRET" in seen["body"]
    assert info["token"] == "NEW" and b.token == "NEW"


def test_upstox_place_order_body_and_errors():
    seen = {}

    def handler(request):
        seen["url"], seen["body"], seen["auth"] = str(request.url), json.loads(request.content), request.headers["authorization"]
        return httpx.Response(200, json={"status": "success", "data": {"order_ids": ["2509270001"]}})
    b = upstox(handler)
    ids = b.place({"symbol": "RELIANCE.NS", "side": "BUY", "qty": 3, "order_type": "LIMIT", "price": 1200.5,
                   "trigger_price": None, "product": "INTRADAY"}, "INE002A01018", market_open=True)
    assert ids == ["2509270001"] and seen["url"] == "https://api-hft.upstox.com/v3/order/place" and seen["auth"] == "Bearer TOK"
    assert seen["body"] == {"quantity": 3, "product": "I", "validity": "DAY", "price": 1200.5, "trigger_price": 0.0,
                            "instrument_token": "NSE_EQ|INE002A01018", "order_type": "LIMIT", "transaction_type": "BUY",
                            "disclosed_quantity": 0, "is_amo": False, "tag": "kairo", "slice": False}
    expired = upstox(lambda r: httpx.Response(401, json={"status": "error", "errors": [{"errorCode": "UDAPI100050", "message": "Invalid token used to access API"}]}))
    with pytest.raises(UpstoxError) as e:
        expired.orders()
    assert e.value.expired and "Invalid token" in str(e.value)


def test_upstox_mappings():
    assert instrument_token("RELIANCE.BO", "INE002A01018") == "BSE_EQ|INE002A01018"
    assert app_symbol("NSE", "RELIANCE-EQ", "NSE_EQ|INE002A01018") == "RELIANCE.NS"
    assert app_symbol("BSE", "RELIANCE", None) == "RELIANCE.BO"
    assert app_symbol("NFO", "NIFTY24OCT22600CE", "NSE_FO|12345") == "NIFTY24OCT22600CE"
    b = upstox(lambda r: httpx.Response(200, json={"status": "success", "data": [
        {"exchange": "NSE", "trading_symbol": "TCS", "instrument_token": "NSE_EQ|INE467B01029", "quantity": 4, "average_price": 2000,
         "last_price": 2100, "close_price": 2050, "pnl": 400, "company_name": "TCS", "product": "D"}]}))
    h = b.holdings()[0]
    assert h["symbol"] == "TCS.NS" and h["invested"] == 8000 and h["value"] == 8400 and h["day_change"] == 200


def test_upstox_token_expiry_rule():
    from api.trading import upstox_expiry
    assert upstox_expiry(at(10, 0)) == dt.datetime(2026, 10, 8, 3, 30, tzinfo=IST)     # next day 03:30
    assert upstox_expiry(at(2, 0)) == dt.datetime(2026, 10, 7, 3, 30, tzinfo=IST)      # same day if before 03:30


# --- GTT / OCO, baskets (phase 3) ---------------------------------------------------------------


def test_gtt_validation_and_hits():
    single = {"kind": "single", "side": "BUY", "trigger": 1900, "direction": tr.gtt_direction(1900, 2000)}
    assert single["direction"] == "below" and tr.gtt_hit(single, 1950) is None and tr.gtt_hit(single, 1899) == "trigger"
    oco = {"kind": "oco", "side": "SELL", "trigger": 2200, "stop_trigger": 1800}
    assert tr.gtt_hit(oco, 2000) is None and tr.gtt_hit(oco, 2210) == "target" and tr.gtt_hit(oco, 1790) == "stop"
    short_oco = {"kind": "oco", "side": "BUY", "trigger": 1800, "stop_trigger": 2200}
    assert tr.gtt_hit(short_oco, 1790) == "target" and tr.gtt_hit(short_oco, 2210) == "stop"
    with pytest.raises(ValueError):
        tr.GttIn(symbol="TCS.NS", kind="oco", side="SELL", qty=1, trigger=2000)                 # no stop
    with pytest.raises(ValueError):
        tr.GttIn(symbol="TCS.NS", kind="oco", side="BUY", qty=1, trigger=2000, stop_trigger=1900)


def test_paper_gtt_single_fires_once(trader):
    t, clock, prices = trader
    g = run(t.create_gtt(tr.GttIn(symbol="TCS.NS", side="BUY", qty=2, trigger=1900)))        # price 2000 -> waits for a dip
    assert g["status"] == "active"
    run(t.tick())
    assert t.gtts()[0]["status"] == "active"
    prices["TCS.NS"] = 1890.0
    run(t.tick()); run(t.tick())
    gt = t.gtts()[0]
    assert gt["status"] == "triggered" and "Trigger hit at 1890" in gt["message"]
    filled = [o for o in t.orders() if o["status"] == "complete"]
    assert len(filled) == 1 and filled[0]["fill_price"] == 1890 and "GTT #" in filled[0]["message"]


def test_paper_oco_stop_cancels_target(trader):
    t, clock, prices = trader
    run(t.place(tr.OrderIn(symbol="TCS.NS", side="BUY", qty=5)))                             # long 5 @ 2000
    run(t.create_gtt(tr.GttIn(symbol="TCS.NS", kind="oco", side="SELL", qty=5, trigger=2200, stop_trigger=1900)))
    prices["TCS.NS"] = 1880.0
    run(t.tick())
    gt = t.gtts()[0]
    assert gt["status"] == "triggered" and "Stop-loss hit" in gt["message"] and "other leg was cancelled" in gt["message"]
    prices["TCS.NS"] = 2300.0
    run(t.tick())                                                                            # the target can't fire any more
    assert sum(1 for o in t.orders() if o["side"] == "SELL") == 1
    assert run(t.positions())[0]["realised"] == -600
    from fastapi import HTTPException
    with pytest.raises(HTTPException):                                                       # price must sit between the legs
        run(t.create_gtt(tr.GttIn(symbol="TCS.NS", kind="oco", side="SELL", qty=1, trigger=2250, stop_trigger=2200)))


def test_paper_gtt_expiry_and_cancel(trader):
    t, clock, prices = trader
    g = run(t.create_gtt(tr.GttIn(symbol="TCS.NS", side="SELL", qty=1, trigger=2500)))
    assert run(t.cancel_gtt(g["id"])) is True and run(t.cancel_gtt(g["id"])) is False
    g2 = run(t.create_gtt(tr.GttIn(symbol="TCS.NS", side="SELL", qty=1, trigger=2500)))
    clock.t = clock.t + dt.timedelta(days=366)
    run(t.tick())
    assert next(x for x in t.gtts() if x["id"] == g2["id"])["status"] == "expired"


def test_paper_basket(trader):
    t, clock, prices = trader
    res = run(t.place_many([tr.OrderIn(symbol="TCS.NS", side="BUY", qty=1), tr.OrderIn(symbol="TCS.NS", side="BUY", qty=10_000),
                            tr.OrderIn(symbol=OPT, side="BUY", qty=65, product="INTRADAY")]))
    assert [r["status"] for r in res] == ["complete", "rejected", "complete"]                 # one rejection doesn't stop the rest


def test_basket_and_gtt_api(trader):
    from api import deps
    from api.main import app
    from api.trading import get_paper, get_upstox
    t, clock, prices = trader
    t.ensure_running = lambda: None
    app.dependency_overrides.update({get_paper: lambda: t, get_upstox: lambda: None, deps.get_store: lambda: t.store})
    try:
        c = TestClient(app)
        b = c.post("/trading/baskets", json={"name": "Two", "orders": [{"symbol": "TCS.NS", "side": "BUY", "qty": 1}]}).json()
        assert b["name"] == "Two" and len(b["orders"]) == 1
        assert c.post("/trading/baskets", json={"name": "Bad", "orders": [{"symbol": "AAPL", "side": "BUY", "qty": 1}]}).status_code == 422
        r = c.post("/trading/basket", json={"orders": b["orders"]}).json()
        assert r["mode"] == "paper" and r["orders"][0]["status"] == "complete"
        assert c.delete(f"/trading/baskets/{b['id']}").status_code == 200 and c.get("/trading/baskets").json()["items"] == []
        g = c.post("/trading/gtt", json={"symbol": "TCS.NS", "side": "BUY", "qty": 1, "trigger": 1900}).json()
        assert g["status"] == "active" and c.get("/trading/gtt").json()["items"][0]["id"] == g["id"]
        assert c.post("/trading/gtt", json={"symbol": "TCS.NS", "kind": "oco", "side": "SELL", "qty": 1, "trigger": 1}).status_code == 422
        assert c.delete(f"/trading/gtt/{g['id']}").status_code == 200
    finally:
        app.dependency_overrides.clear()
