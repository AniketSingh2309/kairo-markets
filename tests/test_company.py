"""Company profiles ("About"): Yahoo session crumb handling, parsing, and the /company endpoint."""

from __future__ import annotations

import httpx
import pytest

from api.market import COMPANIES
from tests.test_platform_api import env  # noqa: F401 - shared fixture
from tools.errors import DataSourceUnavailableError, UnknownSymbolError
from tools.market_tools import get_company
from tools.providers.yahoo_provider import YahooFinanceProvider


def summary(symbol="TCS.NS"):
    return {"quoteSummary": {"error": None, "result": [{
        "assetProfile": {
            "longBusinessSummary": "Tata Consultancy Services Limited provides IT services.",
            "website": "https://www.tcs.com", "sector": "Technology", "industry": "Information Technology Services",
            "fullTimeEmployees": 584519, "city": "Mumbai", "country": "India",
            "companyOfficers": [{"name": "Mr. K.  Krithivasan B.E", "title": "MD,  CEO", "yearBorn": 1965},
                                {"name": "  ", "title": "nobody"}]},
        "quoteType": {"symbol": symbol, "quoteType": "EQUITY", "longName": "Tata Consultancy Services Limited"},
        "price": {"marketCap": {"raw": 7.58e12, "fmt": "7.58T"}, "currency": "INR",
                  "longName": "Tata Consultancy Services Limited"}}]}}


def yahoo(handler_extra=None, crumbs=("c1", "c2")):
    log: list[str] = []
    issued = iter(crumbs)

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        log.append(f"{url.host}{url.path}?crumb={url.params.get('crumb', '')}")
        if url.host == "fc.yahoo.com":
            return httpx.Response(404, headers={"set-cookie": "A3=abc; Domain=.yahoo.com; Path=/"})
        if url.path == "/v1/test/getcrumb":
            if request.headers.get("accept") == "application/json":
                return httpx.Response(406)
            return httpx.Response(200, text=next(issued))
        if handler_extra and (resp := handler_extra(request)) is not None:
            return resp
        return httpx.Response(200, json=summary(url.path.rsplit("/", 1)[-1]))
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://query1.finance.yahoo.com",
                          headers={"Accept": "application/json"})
    return YahooFinanceProvider(client=client), log


def test_company_profile_is_parsed_and_the_crumb_reused():
    prov, log = yahoo()
    d = get_company("TCS.NS", provider=prov, timeout_s=5).data
    assert d.name == "Tata Consultancy Services Limited" and d.description.startswith("Tata Consultancy")
    assert (d.website, d.employees, d.city, d.country, d.market_cap) == ("https://www.tcs.com", 584519, "Mumbai", "India", 7.58e12)
    assert [o.name for o in d.officers] == ["Mr. K. Krithivasan B.E"] and d.officers[0].title == "MD, CEO"  # spaces tidied, blanks dropped
    get_company("AAPL", provider=prov, timeout_s=5)
    assert sum("getcrumb" in line for line in log) == 1                     # one session for many companies
    assert all("crumb=c1" in line for line in log if "quoteSummary" in line)


def test_expired_crumb_is_renewed_once():
    seen = {"n": 0}

    def reject_first(request):
        if "quoteSummary" in request.url.path and request.url.params.get("crumb") == "c1":
            seen["n"] += 1
            return httpx.Response(401, json={"finance": {"error": {"description": "Invalid Crumb"}}})
        return None
    prov, log = yahoo(reject_first)
    assert get_company("TCS.NS", provider=prov, timeout_s=5).data.employees == 584519
    assert seen["n"] == 1 and sum("getcrumb" in line for line in log) == 2


def test_unknown_company_and_dead_session():
    prov, _ = yahoo(lambda r: httpx.Response(404) if "ZZZZ" in r.url.path else None)
    with pytest.raises(UnknownSymbolError):
        get_company("ZZZZ", provider=prov, timeout_s=5)
    dead, _ = yahoo(lambda r: None, crumbs=("<html>",))
    with pytest.raises(DataSourceUnavailableError):
        get_company("TCS.NS", provider=dead, timeout_s=5)


def test_company_endpoint(env):  # noqa: F811
    client, _, _ = env
    COMPANIES._data.clear()
    body = client.get("/company/AAPL").json()
    assert body["symbol"] == "AAPL" and body["sector"] and "officers" in body and body["source"]
    assert client.get("/company/ZZZZ").status_code == 404
    assert client.get("/company/not%20a%20symbol").status_code == 422
