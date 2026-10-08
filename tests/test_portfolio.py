"""Portfolio book-keeping, XIRR, Indian tax estimate and CSV import."""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from core.finance import xirr
from core.importers import parse_csv
from core.portfolio import QuoteLite, Transaction, TransactionIn, build_book, value_portfolio
from core.tax_india import (
    add_months,
    category_of,
    fy_of,
    fy_report,
    is_long_term,
    tax_planning,
)

D = dt.date


def txn(i, symbol, side, qty, price, day, fees=0.0, currency="INR"):
    return Transaction(id=i, symbol=symbol, side=side, quantity=qty, price=price, fees=fees,
                       trade_date=day, currency=currency)


# --- XIRR ---------------------------------------------------------------------------------


def test_xirr_one_year_ten_percent():
    assert xirr([(D(2024, 1, 1), -1000), (D(2024, 12, 31), 1100)]) == pytest.approx(0.1, abs=1e-3)


def test_xirr_matches_excel_reference():
    # Excel: XIRR({-10000, 2750, 4250, 3250, 2750}, {1-Jan-08, 1-Mar-08, 30-Oct-08, 15-Feb-09, 1-Apr-09}) = 37.34%
    flows = [(D(2008, 1, 1), -10000), (D(2008, 3, 1), 2750), (D(2008, 10, 30), 4250),
             (D(2009, 2, 15), 3250), (D(2009, 4, 1), 2750)]
    assert xirr(flows) == pytest.approx(0.373363, abs=1e-4)


def test_xirr_losses_and_undefined_cases():
    assert xirr([(D(2024, 1, 1), -1000), (D(2025, 1, 1), 500)]) == pytest.approx(-0.5, abs=1e-3)
    assert xirr([(D(2024, 1, 1), -1000)]) is None
    assert xirr([(D(2024, 1, 1), -1000), (D(2024, 2, 1), -10)]) is None
    assert xirr([(D(2024, 1, 1), -1000), (D(2024, 1, 1), 1100)]) is None  # same day


# --- FIFO book ----------------------------------------------------------------------------


def test_fifo_matching_and_fees():
    book = build_book([
        txn(1, "INFY.NS", "buy", 10, 100, D(2023, 1, 10), fees=10),   # unit cost 101
        txn(2, "INFY.NS", "buy", 10, 120, D(2023, 6, 10)),            # unit cost 120
        txn(3, "INFY.NS", "sell", 15, 150, D(2024, 2, 1), fees=15),   # unit proceeds 149
    ])
    first, second = book.realized
    assert (first.quantity, first.unit_cost, first.gain) == (10, 101, pytest.approx(480))
    assert (second.quantity, second.unit_cost, second.gain) == (5, 120, pytest.approx(145))
    lots = book.symbols["INFY.NS"].lots
    assert len(lots) == 1 and lots[0].quantity == 5 and lots[0].unit_cost == 120
    assert not book.issues


def test_same_day_buy_before_sell_and_oversell_is_flagged():
    book = build_book([
        txn(2, "TCS.NS", "sell", 5, 110, D(2024, 3, 1)),
        txn(1, "TCS.NS", "buy", 5, 100, D(2024, 3, 1)),
        txn(3, "TCS.NS", "sell", 3, 120, D(2024, 4, 1)),
    ])
    assert book.realized[0].gain == pytest.approx(50)
    assert len(book.issues) == 1 and "exceeds" in book.issues[0].message


def test_valuation_groups_by_currency_with_xirr_and_day_change():
    book = build_book([
        txn(1, "INFY.NS", "buy", 10, 100, D(2025, 1, 1)),
        txn(2, "AAPL", "buy", 2, 200, D(2025, 1, 1), currency="USD"),
    ])
    view = value_portfolio(book, {
        "INFY.NS": QuoteLite(price=110, previous_close=105, currency="INR"),
        "AAPL": QuoteLite(price=250, previous_close=260, currency="USD"),
    }, today=D(2026, 1, 1), fx_to_base={"USD": 90.0})
    inr = next(g for g in view.groups if g.currency == "INR")
    usd = next(g for g in view.groups if g.currency == "USD")
    assert (inr.value, inr.pnl, inr.day_change) == (1100, 100, 50)
    assert inr.xirr == pytest.approx(0.10, abs=2e-3)
    assert (usd.value, usd.day_change) == (500, -20)
    assert view.total_value_in_base == pytest.approx(1100 + 500 * 90)
    assert {p.symbol: round(p.weight) for p in view.positions} == {"INFY.NS": 100, "AAPL": 100}


def test_missing_quote_is_reported_not_guessed():
    book = build_book([txn(1, "ITC.NS", "buy", 1, 400, D(2025, 1, 1))])
    view = value_portfolio(book, {}, today=D(2026, 1, 1))
    assert view.missing_quotes == ["ITC.NS"] and view.positions[0].value is None


def test_transaction_validation():
    with pytest.raises(ValidationError):
        TransactionIn(symbol="^NSEI", side="buy", quantity=1, price=1, trade_date=D(2025, 1, 1))
    with pytest.raises(ValidationError):
        TransactionIn(symbol="INFY.NS", side="buy", quantity=0, price=1, trade_date=D(2025, 1, 1))
    with pytest.raises(ValidationError):
        TransactionIn(symbol="INFY.NS", side="buy", quantity=1, price=1, trade_date=D(2999, 1, 1))


# --- Indian tax -----------------------------------------------------------------------------


def test_categories_and_financial_year():
    assert [category_of(s) for s in ("TCS.NS", "SBIN.BO", "AAPL", "BTC-USD", "ETH-INR")] == [
        "indian_equity", "indian_equity", "foreign_equity", "vda", "vda"]
    assert fy_of(D(2025, 3, 31)) == "FY 2024-25" and fy_of(D(2025, 4, 1)) == "FY 2025-26"


def test_holding_period_is_calendar_based():
    assert add_months(D(2024, 1, 31), 1) == D(2024, 2, 29)
    assert not is_long_term("indian_equity", D(2024, 3, 1), D(2025, 3, 1))  # exactly 12 months
    assert is_long_term("indian_equity", D(2024, 3, 1), D(2025, 3, 2))
    assert not is_long_term("foreign_equity", D(2024, 3, 1), D(2025, 6, 1))


def test_rate_change_on_23_july_2024():
    book = build_book([
        txn(1, "A.NS", "buy", 1, 100, D(2024, 1, 1)), txn(2, "A.NS", "sell", 1, 200, D(2024, 7, 22)),  # STCG 15%
        txn(3, "B.NS", "buy", 1, 100, D(2024, 1, 1)), txn(4, "B.NS", "sell", 1, 300, D(2024, 7, 23)),  # STCG 20%
    ])
    ind = fy_report(book, "FY 2024-25").indian
    assert {b.rate: b.gains for b in ind.st_buckets} == {0.15: 100, 0.20: 200}
    assert ind.tax == pytest.approx(100 * 0.15 + 200 * 0.20)
    assert ind.total_tax == pytest.approx(ind.tax * 1.04)


def test_ltcg_exemption_and_loss_setoff():
    book = build_book([
        # LTCG 2,00,000 @12.5%
        txn(1, "L.NS", "buy", 100, 1000, D(2023, 1, 1)), txn(2, "L.NS", "sell", 100, 3000, D(2025, 6, 1)),
        # STCL 30,000 (sets off against LTCG since there is no STCG)
        txn(3, "S.NS", "buy", 100, 1000, D(2025, 5, 1)), txn(4, "S.NS", "sell", 100, 700, D(2025, 9, 1)),
    ])
    ind = fy_report(book, "FY 2025-26").indian
    assert (ind.ltcg, ind.stcl, ind.net_ltcg) == (200_000, 30_000, 170_000)
    assert ind.exemption_used == 125_000
    lt = ind.lt_buckets[0]
    assert (lt.rate, lt.taxable) == (0.125, 45_000)
    assert ind.total_tax == pytest.approx(45_000 * 0.125 * 1.04)


def test_long_term_loss_cannot_offset_short_term_gain():
    book = build_book([
        txn(1, "S.NS", "buy", 1, 100, D(2025, 5, 1)), txn(2, "S.NS", "sell", 1, 1100, D(2025, 8, 1)),   # STCG 1000
        txn(3, "L.NS", "buy", 1, 1000, D(2023, 1, 1)), txn(4, "L.NS", "sell", 1, 500, D(2025, 8, 1)),   # LTCL 500
    ])
    ind = fy_report(book, "FY 2025-26").indian
    assert ind.net_stcg == 1000 and ind.tax == pytest.approx(200)


def test_crypto_and_foreign_are_reported_separately():
    book = build_book([
        txn(1, "BTC-INR", "buy", 1, 100, D(2025, 5, 1)), txn(2, "BTC-INR", "sell", 1, 200, D(2025, 6, 1)),
        txn(3, "ETH-INR", "buy", 1, 100, D(2025, 5, 1)), txn(4, "ETH-INR", "sell", 1, 50, D(2025, 6, 1)),
        txn(5, "AAPL", "buy", 1, 100, D(2025, 5, 1), currency="USD"),
        txn(6, "AAPL", "sell", 1, 150, D(2025, 6, 1), currency="USD"),
    ])
    rep = fy_report(book, "FY 2025-26")
    vda = rep.vda[0]
    assert (vda.gains, vda.losses_ignored, vda.tax) == (100, 50, pytest.approx(30))
    assert rep.foreign[0].currency == "USD" and rep.foreign[0].stcg == 50
    assert rep.indian is None
    assert rep.estimated_total_tax_inr == pytest.approx(30 * 1.04)


def test_tax_planning_headroom_and_lots_turning_long_term():
    today = D(2025, 10, 1)
    book = build_book([
        txn(1, "OLD.NS", "buy", 10, 100, D(2023, 1, 1)),
        txn(2, "NEW.NS", "buy", 5, 100, D(2024, 10, 15)),  # turns long-term on 16-Oct-2025
    ])
    plan = tax_planning(book, {"OLD.NS": 300, "NEW.NS": 150}, today)
    assert plan.exemption_remaining == 125_000
    assert plan.harvest_ideas[0].symbol == "OLD.NS" and plan.harvest_ideas[0].unrealized_gain == 2000
    soon = plan.turning_long_term_soon[0]
    assert (soon.symbol, soon.turns_long_term_on, soon.days_left) == ("NEW.NS", D(2025, 10, 16), 15)


# --- CSV import -------------------------------------------------------------------------------


def test_generic_csv_import():
    res = parse_csv("Date,Symbol,Side,Qty,Price,Fees\n2025-01-10,infy.ns,BUY,10,1500.5,20\n"
                    "10/02/2025,TCS.NS,sell,2,\"4,100\",\nbad-date,X.NS,buy,1,1,\n")
    assert res.format == "generic" and len(res.rows) == 2
    assert res.rows[0].symbol == "INFY.NS" and res.rows[0].fees == 20
    assert res.rows[1].trade_date == D(2025, 2, 10) and res.rows[1].price == 4100
    assert res.errors[0].line == 4


def test_zerodha_tradebook_import():
    text = ("symbol,isin,trade_date,exchange,segment,series,trade_type,auction,quantity,price,trade_id,order_id,order_execution_time\n"
            "RELIANCE,INE002A01018,2025-03-12,NSE,EQ,EQ,buy,false,5.000000,1250.50,1,2,2025-03-12T10:01:02\n"
            "NIFTY25MARFUT,,2025-03-12,NFO,FO,,buy,false,50,22000,3,4,2025-03-12T10:02:00\n"
            "SBIN,INE062A01020,2025-04-01,BSE,EQ,A,sell,false,3,780,5,6,2025-04-01T11:00:00\n")
    res = parse_csv(text)
    assert res.format == "zerodha" and res.skipped == 1 and not res.errors
    assert [(r.symbol, r.side, r.quantity) for r in res.rows] == [("RELIANCE.NS", "buy", 5), ("SBIN.BO", "sell", 3)]


def test_csv_with_missing_columns_explains_itself():
    res = parse_csv("name,amount\nfoo,1\n")
    assert not res.rows and "missing column" in res.errors[0].message
