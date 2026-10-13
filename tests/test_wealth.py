"""Tax-harvesting plans and the Regular vs Direct mutual-fund comparison."""

import datetime as dt
from types import SimpleNamespace

from core import wealth
from core.portfolio import Transaction, build_book

TODAY = dt.date(2026, 10, 9)


def txn(i, sym, side, qty, price, day):
    return Transaction(id=i, symbol=sym, side=side, quantity=qty, price=price, trade_date=day, created_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))


def test_ltcg_harvest_uses_fifo_long_term_lots_within_exemption():
    book = build_book([txn(1, "TCS.NS", "buy", 100, 1000, dt.date(2024, 1, 10)),       # long-term, +1,000/share at 2,000
                       txn(2, "TCS.NS", "buy", 50, 1900, dt.date(2026, 6, 1)),         # short-term
                       txn(3, "INFY.NS", "buy", 10, 1500, dt.date(2025, 1, 1))])      # long-term, in loss at 1,400
    p = wealth.harvest_plan(book, {"TCS.NS": 2000.0, "INFY.NS": 1400.0}, TODAY, exemption_remaining=125000,
                            realised_stcg=0, realised_ltcg_taxable=0)
    g = p["gains"][0]
    assert g["symbol"] == "TCS.NS" and g["quantity"] == 100 and g["gain"] == 100000      # all 100 old shares fit in the exemption
    assert g["future_tax_saved"] == 12500 and p["losses"] == []                         # no taxable gains to offset
    p2 = wealth.harvest_plan(book, {"TCS.NS": 2000.0, "INFY.NS": 1400.0}, TODAY, exemption_remaining=30000, realised_stcg=5000, realised_ltcg_taxable=0)
    assert p2["gains"][0]["quantity"] == 30 and p2["gains"][0]["gain"] == 30000           # capped by what's left of the exemption
    assert p2["losses"][0]["symbol"] == "INFY.NS" and p2["losses"][0]["loss"] == 1000
    assert p2["loss_tax_saved"] == 0                                                      # a long-term loss can't offset short-term gains
    book2 = build_book([txn(1, "Y.NS", "buy", 10, 200, dt.date(2026, 8, 1))])             # short-term lot, 500 under water
    p3 = wealth.harvest_plan(book2, {"Y.NS": 150.0}, TODAY, 125000, realised_stcg=5000, realised_ltcg_taxable=0)
    assert p3["losses"][0]["short_term_loss"] == 500 and p3["loss_tax_saved"] == 100.0    # 500 x 20% STCG rate


def test_harvest_skips_when_oldest_lot_is_short_term_or_in_loss():
    book = build_book([txn(1, "X.NS", "buy", 10, 100, dt.date(2026, 5, 1))])
    assert wealth.harvest_plan(book, {"X.NS": 150.0}, TODAY, 125000, 0, 0)["gains"] == []


def test_direct_gap_and_switch_value():
    days = [dt.date(2022, 10, 9) + dt.timedelta(days=i) for i in range(0, 1462, 7)]
    reg = [(d, 100 * 1.11 ** ((d - days[0]).days / 365.25)) for d in days]
    dirc = [(d, 100 * 1.12 ** ((d - days[0]).days / 365.25)) for d in days]
    g = wealth.annual_gap(reg, dirc)
    assert abs(g["gap"] - 1.0) < 0.05 and g["years"] == 3.0
    v = wealth.switch_value(1_000_000, 1.0, 3)
    assert 25_000 < v["lost_so_far"] < 35_000 and 250_000 < v["next_years_cost"] < 300_000
    assert wealth.annual_gap(reg[:10], dirc[:10]) is None                                   # under a year of overlap
    s = SimpleNamespace(code="1", name="Fund A", plan="Regular", option="Growth")
    sib = [SimpleNamespace(code="2", name="Fund A", plan="Direct", option="Growth"), SimpleNamespace(code="3", name="Fund A", plan="Direct", option="IDCW")]
    assert wealth.plan_sibling(s, [s, *sib]).code == "2"


def test_record_harvest_api():
    from fastapi.testclient import TestClient
    from api import deps
    from api.main import app
    from core.store import Store
    store = Store(":memory:")
    store.add_transactions([{"symbol": "TCS.NS", "side": "buy", "quantity": 10, "price": 100, "trade_date": "2024-01-01", "currency": "INR"}])
    app.dependency_overrides[deps.get_store] = lambda: store
    try:
        c = TestClient(app)
        assert c.post("/portfolio/harvest/record", json={"items": [{"symbol": "TCS.NS", "quantity": 20, "price": 150}]}).status_code == 422
        r = c.post("/portfolio/harvest/record", json={"items": [{"symbol": "TCS.NS", "quantity": 10, "price": 150}]})
        assert r.json()["recorded"] == 2
        sides = [(t["side"], t["note"]) for t in store.list_transactions()[1:]]
        assert sides == [("sell", "Tax harvesting: sale"), ("buy", "Tax harvesting: buy-back")]
        book = build_book([Transaction(**{**t, "trade_date": t["trade_date"]}) for t in store.list_transactions()])
        assert book.symbols["TCS.NS"].lots[0].unit_cost == 150                               # cost basis reset
    finally:
        app.dependency_overrides.clear()
