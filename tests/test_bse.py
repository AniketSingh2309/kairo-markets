"""BSE: the company list, BSE universes, NSE<->BSE pairing, search by scrip code, and corporate filings."""

from __future__ import annotations

import json

import httpx
import pytest

from core.universes import Universes
from tools.providers.base import NoDataAvailable, ProviderUnavailable
from tools.providers.bse import BseFilings, BseFilingsProvider, parse_filings
from tools.providers.listings import ListingStore, parse_bse_equity

NSE_EQ = """SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE
RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995,10,1,INE002A01018,10
M&M,Mahindra & Mahindra Limited,EQ,29-NOV-1995,5,1,INE101A01026,5
"""
BSE_LIST = [
    {"SCRIP_CD": "500325", "Scrip_Name": "Reliance Ind", "Status": "Active", "GROUP": "A", "ISIN_NUMBER": "INE002A01018",
     "scrip_id": "RELIANCE", "Issuer_Name": "Reliance Industries Limited", "Mktcap": "1600000.00"},
    {"SCRIP_CD": "500520", "Scrip_Name": "M&M", "Status": "Active", "GROUP": "A", "ISIN_NUMBER": "INE101A01026",
     "scrip_id": "M&M", "Issuer_Name": "Mahindra & Mahindra Limited", "Mktcap": "330000.00"},
    {"SCRIP_CD": "500012", "Scrip_Name": "Andhra Petro", "Status": "Active", "GROUP": "X", "ISIN_NUMBER": "INE714B01016",
     "scrip_id": "ANDHRAPET", "Issuer_Name": "ANDHRA PETROCHEMICALS LTD", "Mktcap": "330.50"},
    {"SCRIP_CD": "590103", "Scrip_Name": "Some ETF", "Status": "Active", "GROUP": "F", "ISIN_NUMBER": "INF204K01ZZ1",
     "scrip_id": "SOMEETF", "Issuer_Name": "Some Mutual Fund", "Mktcap": "9999999.00"},
    {"SCRIP_CD": "500001", "Scrip_Name": "Gone", "Status": "Delisted", "ISIN_NUMBER": "INE000X01010", "scrip_id": "GONE"},
]


def filings_payload(n=2, total=None):
    rows = [{"NEWSID": f"id{i}", "NEWSSUB": "Announcement under Regulation 30 (LODR)-Allotment" if i == 0 else f"Board Meeting {i}",
             "NEWS_DT": f"2026-10-0{1 + i % 5}T09:15:04.317", "CATEGORYNAME": "Company Update", "SUBCATNAME": "General",
             "HEADLINE": "Please find  enclosed the Scrutinizer''s report.", "MORE": "",
             "ATTACHMENTNAME": f"file{i}.pdf"} for i in range(n)]
    return {"Table": rows, "Table1": [{"ROWCNT": total if total is not None else n}]}


@pytest.fixture
def store(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "EQUITY_L" in url:
            return httpx.Response(200, text=NSE_EQ)
        if "ListofScripData" in url:
            assert request.headers.get("referer") == "https://www.bseindia.com/"  # BSE denies requests without it
            return httpx.Response(200, json=BSE_LIST)
        return httpx.Response(404)
    return ListingStore(cache_dir=tmp_path, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_bse_list_parsing():
    rows = {r.symbol: r for r in parse_bse_equity(json.dumps(BSE_LIST))}
    assert set(rows) == {"RELIANCE.BO", "M&M.BO", "ANDHRAPET.BO", "SOMEETF.BO"}       # delisted dropped
    assert rows["RELIANCE.BO"].code == "500325" and rows["RELIANCE.BO"].market_cap == pytest.approx(1.6e13)
    assert rows["ANDHRAPET.BO"].name == "Andhra Petrochemicals Ltd" and rows["SOMEETF.BO"].kind == "etf"


def test_bse_universes_venues_and_code_search(store):
    u = Universes(store)
    assert u.get("bse_top100").symbols == ["RELIANCE.BO", "M&M.BO", "ANDHRAPET.BO"]   # ETFs aren't companies
    assert u.get("bse_only").symbols == ["ANDHRAPET.BO"]                              # not listed on NSE
    assert u.venues("M&M.NS") == {"NSE": "M&M.NS", "BSE": "M&M.BO", "isin": "INE101A01026", "bse_code": "500520"}
    assert u.venues("ANDHRAPET.BO")["NSE"] is None and u.venues("AAPL")["BSE"] is None
    assert [h.symbol for h in u.search("500325")] == ["RELIANCE.NS"]                  # dual-listed -> NSE line
    assert [h.symbol for h in u.search("500012")] == ["ANDHRAPET.BO"]
    assert u.search("andhra")[0].symbol == "ANDHRAPET.BO"                              # BSE-only names are searchable
    assert [h.symbol for h in u.search("reliance")] == ["RELIANCE.NS"]                # listed once, not twice


def test_filings_parsing():
    items = parse_filings(filings_payload(2))
    assert items[0]["title"] == "Allotment" and items[0]["published_at"].endswith("+05:30")
    assert items[0]["url"].endswith("/AttachLive/file0.pdf") and items[0]["category"].startswith("filing: BSE · Company Update")
    assert items[0]["summary"] == "Please find enclosed the Scrutinizer's report."     # whitespace and '' cleaned
    with pytest.raises(ValueError):
        parse_filings({"oops": 1})


def test_filings_pages_until_done():
    pages: list[str] = []

    def handler(request):
        pages.append(request.url.params["pageno"])
        return httpx.Response(200, json=filings_payload(50 if request.url.params["pageno"] == "1" else 3, total=53))
    got = BseFilings(client=httpx.Client(transport=httpx.MockTransport(handler))).fetch("500325", 30)
    assert pages == ["1", "2"] and len(got) == 53


class FakeMarket:
    name = "fake"

    def __init__(self, fail: Exception | None = None):
        self.fail = fail

    def fetch_announcements(self, symbol, lookback_days):
        if self.fail:
            raise self.fail
        return {"meta": {"source_id": "yahoo", "url": "u", "observed_at": "2026-10-07T00:00:00+00:00"},
                "data": {"symbol": symbol, "items": [{"id": "n1", "published_at": "2026-10-02T04:00:00+00:00",
                                                       "category": "news: Reuters", "title": "News", "url": "x"}]}}

    def fetch_quote(self, symbol, on_date):
        return "passthrough"


class FakeFilings:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def fetch(self, code, days):
        self.calls.append(code)
        if self.fail:
            raise ProviderUnavailable("BSE down")
        return parse_filings(filings_payload(2))


def test_filings_merge_into_indian_news_only():
    filings = FakeFilings()
    p = BseFilingsProvider(FakeMarket(), filings, lambda s: "500520" if s.startswith("M&M") else None)
    items = p.fetch_announcements("M&M.NS", 30)["data"]["items"]
    # 04:00 UTC is 09:30 IST, later than the 09:15 IST filing: sorted by instant, not by text
    assert [i["id"] for i in items] == ["n1", "bse:id1", "bse:id0"]
    assert p.fetch_announcements("AAPL", 30)["data"]["items"][0]["id"] == "n1" and filings.calls == ["500520"]
    assert p.fetch_quote("M&M.NS", None) == "passthrough"                              # everything else untouched


def test_filings_fallbacks():
    down = BseFilingsProvider(FakeMarket(), FakeFilings(fail=True), lambda s: "500520")
    assert [i["id"] for i in down.fetch_announcements("M&M.NS", 30)["data"]["items"]] == ["n1"]   # BSE down: news stays
    no_news = BseFilingsProvider(FakeMarket(fail=NoDataAvailable("none")), FakeFilings(), lambda s: "500520")
    assert len(no_news.fetch_announcements("M&M.NS", 30)["data"]["items"]) == 2                  # filings alone
    both = BseFilingsProvider(FakeMarket(fail=NoDataAvailable("none")), FakeFilings(fail=True), lambda s: "500520")
    with pytest.raises(NoDataAvailable):
        both.fetch_announcements("M&M.NS", 30)
