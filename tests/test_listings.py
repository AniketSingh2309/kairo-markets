"""Official stock lists, universes built from them, and instrument search (all offline, via a fake transport)."""

from __future__ import annotations

import json
import os
import time

import httpx
import pytest

from core.universes import Universes
from tools.models import normalize_symbol
from tools.providers.listings import (ListingStore, ListingUnavailable, parse_crypto, parse_nasdaq100,
                                      parse_nifty_index, parse_nse_equity, parse_nse_etf, parse_us_stocks)

NSE_EQ = """SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE
20MICRONS,20 Microns Limited,EQ,06-OCT-2008,5,1,INE144J01027,5
INFY,Infosys Limited,EQ,08-FEB-1995,5,1,INE009A01021,5
M&M,Mahindra & Mahindra Limited,EQ,29-NOV-1995,5,1,INE101A01026,5
BAJAJ-AUTO,Bajaj Auto Limited,EQ,26-MAY-2008,10,1,INE917I01010,10
INFOBEAN,InfoBeans Technologies Limited,EQ,01-JAN-2020,10,1,INE344S01016,10
"""
NSE_ETF = """Symbol,Underlying Asset,SecurityName,DateofListing,MarketLot,ISINNumber,FaceValue,ETF Underlying,Underlying Key
NIFTYBEES,Nifty 50,NIPINDETFNIFTYBEES,08-Jan-02,1,INF204KB14I2,1,EQUITY,Nifty 50
"""
NIFTY50 = """Company Name,Industry,Symbol,Series,ISIN Code
Infosys Ltd.,Information Technology,INFY,EQ,INE009A01021
Mahindra & Mahindra Ltd.,Automobile and Auto Components,M&M,EQ,INE101A01026
Dummy Hegde Ltd.,Capital Goods,DUMMYHEG,EQ,INE000000000
"""
NIFTYBANK = """Company Name,Industry,Symbol,Series,ISIN Code
HDFC Bank Ltd.,Financial Services,HDFCBANK,EQ,INE040A01034
"""


def us_rows():
    rows = [
        ("AAPL", "Apple Inc. Common Stock", "4000000000000", "Technology"),
        ("GOOGL", "Alphabet Inc. Class A Common Stock", "2000000000000", "Technology"),
        ("GOOG", "Alphabet Inc. Class C Capital Stock", "1990000000000", "Technology"),
        ("BRK/B", "Berkshire Hathaway Inc.", "1100000000000", ""),
        ("ABR^D", "Arbor Realty Preferred", "500000000", "Real Estate"),
        ("JPM", "JPMorgan Chase & Co. Common Stock", "700000000000", "Finance"),
        ("TINY", "Tiny Corp Common Stock", "0.00", "Technology"),
    ]
    return {"data": {"rows": [{"symbol": s, "name": n, "marketCap": c, "sector": sec, "industry": ""} for s, n, c, sec in rows]}}


NASDAQ100 = {"data": {"data": {"rows": [{"symbol": "AAPL"}, {"symbol": "GOOGL"}, {"symbol": "XYZ"}]}}}
CRYPTO = {"finance": {"result": [{"quotes": [
    {"symbol": "BTC-USD", "shortName": "Bitcoin USD", "marketCap": 1.7e12, "regularMarketPrice": 60000},
    {"symbol": "USDT-USD", "shortName": "Tether USDt USD", "marketCap": 1e11, "regularMarketPrice": 1.0},
    {"symbol": "WBTC-USD", "shortName": "Wrapped Bitcoin USD", "marketCap": 1e10, "regularMarketPrice": 60000},
    {"symbol": "ETH-USD", "shortName": "Ethereum USD", "marketCap": 4e11, "regularMarketPrice": 3000},
]}]}}


def fake_transport(calls: list[str], fail: set[str] = frozenset()):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if any(f in url for f in fail):
            return httpx.Response(503, text="down")
        if "EQUITY_L" in url:
            return httpx.Response(200, text=NSE_EQ)
        if "eq_etfseclist" in url:
            return httpx.Response(200, text=NSE_ETF)
        if "ind_nifty50list" in url:
            return httpx.Response(200, text=NIFTY50)
        if "ind_niftybanklist" in url:
            return httpx.Response(200, text=NIFTYBANK)
        if "niftyindices" in url:
            return httpx.Response(200, text="<!DOCTYPE html><html>not found</html>")  # their 'missing' page is a 200
        if "screener/stocks" in url:
            return httpx.Response(200, json=us_rows())
        if "nasdaq100" in url:
            return httpx.Response(200, json=NASDAQ100)
        if "cryptocurrencies" in url:
            return httpx.Response(200, json=CRYPTO)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


@pytest.fixture
def store(tmp_path):
    calls: list[str] = []
    st = ListingStore(cache_dir=tmp_path, client=httpx.Client(transport=fake_transport(calls)))
    st.calls = calls  # type: ignore[attr-defined]
    return st


# --- symbols -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("sym", ["M&M.NS", "BAJAJ-AUTO.NS", "20MICRONS.NS", "J&KBANK.NS", "BRK-B", "HYPE32196-USD"])
def test_real_exchange_symbols_are_accepted(sym):
    assert normalize_symbol(sym.lower()) == sym


# --- parsers -----------------------------------------------------------------------------------------


def test_parsers():
    eq = parse_nse_equity(NSE_EQ)
    assert [x.symbol for x in eq] == ["20MICRONS.NS", "INFY.NS", "M&M.NS", "BAJAJ-AUTO.NS", "INFOBEAN.NS"]
    assert eq[1].name == "Infosys Limited" and eq[1].isin == "INE009A01021"
    assert parse_nse_etf(NSE_ETF)[0].name == "Nifty 50 ETF"
    n50 = parse_nifty_index(NIFTY50)
    assert n50[1].symbol == "M&M.NS" and n50[1].sector == "Automobile and Auto Components"
    with pytest.raises(ValueError):
        parse_nifty_index("<!DOCTYPE html>")
    us = {x.symbol: x for x in parse_us_stocks(json.dumps(us_rows()))}
    assert "BRK-B" in us and "ABR^D" not in us and not any("^" in s for s in us)  # preferreds skipped
    assert us["AAPL"].name == "Apple Inc." and us["GOOGL"].name == "Alphabet Inc." and us["TINY"].market_cap is None
    assert parse_nasdaq100(json.dumps(NASDAQ100)) == ["AAPL", "GOOGL", "XYZ"]
    assert [c.symbol for c in parse_crypto(json.dumps(CRYPTO))] == ["BTC-USD", "ETH-USD"]  # no stablecoin / wrapped


# --- store: caching and fallbacks ------------------------------------------------------------------------


def test_store_caches_on_disk_and_refreshes_weekly(store, tmp_path):
    assert len(store.nse_equities()) == 5
    assert len(store.nse_equities()) == 5 and len(store.calls) == 1  # memory
    again = ListingStore(cache_dir=tmp_path, client=httpx.Client(transport=fake_transport(calls := [])))
    assert len(again.nse_equities()) == 5 and calls == []            # disk, no network
    old = time.time() - 8 * 24 * 3600
    os.utime(tmp_path / "nse_equity.txt", (old, old))
    third = ListingStore(cache_dir=tmp_path, client=httpx.Client(transport=fake_transport(calls3 := [])))
    third.nse_equities()
    assert len(calls3) == 1                                            # a week old: refreshed


def test_store_serves_last_good_copy_when_publisher_is_down(tmp_path):
    ok = ListingStore(cache_dir=tmp_path, client=httpx.Client(transport=fake_transport([])))
    ok.nse_equities()
    old = time.time() - 8 * 24 * 3600
    os.utime(tmp_path / "nse_equity.txt", (old, old))
    down = ListingStore(cache_dir=tmp_path, client=httpx.Client(transport=fake_transport([], fail={"EQUITY_L"})))
    assert len(down.nse_equities()) == 5 and down.status()["nse_equity"]["stale"] is True
    empty = ListingStore(cache_dir=tmp_path / "none", client=httpx.Client(transport=fake_transport([], fail={"EQUITY_L"})))
    with pytest.raises(ListingUnavailable):
        empty.nse_equities()


def test_error_page_never_replaces_a_list(store):
    with pytest.raises(ListingUnavailable):
        store.nifty("niftymedia")  # niftyindices answers 200 with an HTML page for missing files
    assert not list(store.cache_dir.glob("nifty_niftymedia*"))


# --- universes -------------------------------------------------------------------------------------------


def test_universes_from_official_lists(store):
    u = Universes(store)
    assert u.default_key == "nifty50"
    n50 = u.get("nifty50")
    assert n50.symbols == ["INFY.NS", "M&M.NS"] and n50.sector("INFY.NS") == "Information Technology"
    assert u.get("niftybank").label == "Nifty Bank"
    top = u.get("us_top100").symbols
    assert top[:3] == ["AAPL", "GOOGL", "BRK-B"] and "GOOG" not in top and "TINY" not in top  # one line per company
    assert u.get("us_technology").symbols == ["AAPL", "GOOGL"]
    assert u.get("nasdaq100").symbols == ["AAPL", "GOOGL", "XYZ"]
    assert u.get("crypto").symbols == ["BTC-USD", "ETH-USD"]
    with pytest.raises(ListingUnavailable):
        u.get("niftymedia")
    with pytest.raises(KeyError):
        u.get("nope")
    counts = {c["key"]: c["count"] for c in u.catalog()}
    assert counts["nifty50"] == 2 and counts["niftymedia"] is None
    assert all(c["source"] for c in u.catalog())


def test_search_ranks_exact_then_prefix_then_name(store):
    u = Universes(store)
    assert [h.symbol for h in u.search("infy")][:1] == ["INFY.NS"]
    assert [h.symbol for h in u.search("info")][:2] == ["INFY.NS", "INFOBEAN.NS"]  # name prefix; Nifty 50 first
    assert u.search("mahindra")[0].symbol == "M&M.NS"
    assert u.search("m&m")[0].symbol == "M&M.NS"
    assert u.search("apple")[0].symbol == "AAPL" and u.search("apple")[0].exchange == "US"
    assert u.search("alphabet")[0].symbol == "GOOGL"                               # bigger class first
    assert u.search("bitcoin")[0].symbol == "BTC-USD"
    assert u.search("nifty 50")[0].kind == "etf"
    assert u.search("") == [] and u.search("zzzzqq") == []
    assert u.search("infosys")[0].sector == "Information Technology"


def test_mock_universes():
    u = Universes.for_mock(["AAPL", "BADF", "MSFT", "SLOW"])
    assert u.default_key == "mock" and u.get("mock").symbols == ["AAPL", "BADF", "MSFT", "SLOW"]
    assert [h.symbol for h in u.search("ms")] == ["MSFT"]
