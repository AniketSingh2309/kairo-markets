"""India market-pulse parsers (NSE): flows, deals, corporate actions, surveillance, bhavcopy, IPOs, participant OI."""

import datetime as dt

from api.india import delivery_rows, nse_symbol
from tools.providers import nse_india as ni


def test_fii_dii_and_deals():
    f = ni.parse_fii_dii([{"buyValue": "22987.97", "category": "DII", "date": "08-Oct-2026", "netValue": "10703.11", "sellValue": "12284.86"},
                          {"buyValue": "15725.45", "category": "FII/FPI", "date": "08-Oct-2026", "netValue": "-12943.58", "sellValue": "28669.03"}])
    assert f["date"] == "2026-10-08" and f["fii"]["net"] == -12943.58 and f["dii"]["buy"] == 22987.97
    d = ni.parse_large_deals({"as_on_date": "08-Oct-2026", "BLOCK_DEALS_DATA": [
        {"buySell": "BUY", "clientName": "X FUND", "date": "08-Oct-2026", "name": "Reliance", "qty": "2040212", "symbol": "RELIANCE", "watp": "1176"}]})
    assert d["block"][0]["value_cr"] == 239.93 and d["bulk"] == [] and d["as_on"] == "2026-10-08"


def test_corporate_actions_and_events():
    rows = ni.parse_corporate_actions([
        {"symbol": "ACCELYA", "comp": "Accelya", "series": "EQ", "subject": "Dividend - Rs 35 Per Share", "exDate": "09-Oct-2026", "recDate": "09-Oct-2026"},
        {"symbol": "ABC", "series": "EQ", "subject": "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share", "exDate": "15-Oct-2026"},
        {"symbol": "706GS2046", "series": "GS", "subject": "Interest Payment", "exDate": "09-Oct-2026"}])
    assert [r["symbol"] for r in rows] == ["ACCELYA", "ABC"]
    assert rows[0]["kind"] == "dividend" and rows[0]["amount"] == 35 and rows[1]["kind"] == "split"
    assert ni.action_kind("Bonus 1:1") == "bonus" and ni.action_kind("Buy Back") == "buyback"
    ev = ni.parse_events([{"symbol": "DMART", "company": "Avenue", "purpose": "Financial Results", "bm_desc": "Q2", "date": "10-Oct-2026"}])
    assert ev[0]["results"] and ev[0]["date"] == "2026-10-10"


def test_surveillance_and_ban():
    s = ni.parse_surveillance({"longterm": {"data": [{"symbol": "A2ZINFRA", "asmSurvIndicator": "Stage I"}]}, "shortterm": {"data": []}},
                              [{"symbol": "AGSTRA", "survDesc": "ASM IBC Stage I and GSM Stage 0", "gsmStage": "LVIII"}])
    assert s["A2ZINFRA"]["asm"] == "Long-term ASM Stage I" and "GSM" in s["AGSTRA"]["gsm"]
    b = ni.parse_ban("Securities in Ban For Trade Date 09-OCT-2026:\n1,AMBUJACEM\n2,BANDHANBNK\n")
    assert b == {"date": "2026-10-09", "symbols": ["AMBUJACEM", "BANDHANBNK"]}


BHAV = """SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER
TCS, EQ, 08-Oct-2026, 100, 100, 110, 99, 108, 108, 104, 1000, {t}, 50, {dq}, {dp}
TCSBOND, N1, 08-Oct-2026, 100, 100, 110, 99, 108, 108, 104, 1000, 10, 50, 10, 1
"""


def test_bhav_and_delivery_rows():
    today = ni.parse_bhav_full(BHAV.format(t=1200, dq=800, dp=80))
    assert set(today) == {"TCS"} and today["TCS"]["turnover_cr"] == 12 and today["TCS"]["delivery_pct"] == 80
    before = [ni.parse_bhav_full(BHAV.format(t=1000, dq=400, dp=40)) for _ in range(3)]
    rows = delivery_rows([(dt.date(2026, 10, 8), today)] + [(dt.date(2026, 10, d), b) for d, b in zip((7, 6, 5), before)])
    r = rows["TCS"]
    assert r["delivery_x"] == 2 and r["avg_delivery_pct"] == 40 and r["change_pct"] == 8 and r["avg_turnover_cr"] == 10


def test_ipos_and_participants():
    today = dt.date(2026, 10, 9)
    out = ni.parse_ipos([{"symbol": "NEW", "companyName": "New Co", "series": "EQ", "noOfTime": "3.5", "issueStartDate": "07-Oct-2026"}],
                        [{"symbol": "NEW"}, {"symbol": "SOON", "companyName": "Soon Ltd", "issueStartDate": "14-Oct-2026"}],
                        [{"symbol": "OLD", "listingDate": "01-Jan-2020"}, {"symbol": "RECENT", "listingDate": "01-Oct-2026"}], today)
    assert out["open"][0]["subscribed_x"] == 3.5 and [x["symbol"] for x in out["upcoming"]] == ["SOON"] and [x["symbol"] for x in out["listed"]] == ["RECENT"]
    csv_text = ('"Participant wise Open Interest (no. of contracts) in Equity Derivatives as on Oct 08, 2026",,,,,,,,,,,,,,\n'
                "Client Type,Future Index Long,Future Index Short,Future Stock Long,Future Stock Short,Option Index Call Long,Option Index Put Long,"
                "Option Index Call Short,Option Index Put Short,Option Stock Call Long,Option Stock Put Long,Option Stock Call Short,Option Stock Put Short,Total Long Contracts,Total Short Contracts\n"
                "FII,31583,338692,1,1,737159,1122843,1141437,456760,1,1,1,1,1,1\nTOTAL,1,1,1,1,1,1,1,1,1,1,1,1,1,1\n")
    p = ni.parse_participant_oi(csv_text)
    assert p["date"] == "2026-10-08" and p["participants"]["FII"]["fut_long_pct"] == 8.5 and "TOTAL" not in p["participants"]


def test_nse_symbol():
    assert nse_symbol("reliance.ns") == "RELIANCE" and nse_symbol("TCS.BO") == "TCS" and nse_symbol("M&M") == "M&M"
