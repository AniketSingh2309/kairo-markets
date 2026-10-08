"""Stock research: fundamentals parsing, scorecard percentiles, peer selection, shareholding XBRL."""

from __future__ import annotations

import time

import httpx
import pytest

from core.scorecard import closest_by_size, percentile, scorecard, usable
from tools.market_tools import get_fundamentals
from tools.providers.nse import parse_shareholding_xbrl
from tools.providers.yahoo_provider import YahooFinanceProvider


def row(**k):
    base = {"year_change": 0.1, "pe": 20, "pb": 3, "roe": 0.15, "net_margin": 0.1, "revenue_growth": 0.1,
            "earnings_growth": 0.1, "beta": 1.0, "debt_to_equity": 0.5}
    return {**base, **k}


def test_percentile_and_direction():
    vals = [10, 20, 30, 40, 50]
    assert percentile(50, vals, 1) == 100 and percentile(10, vals, 1) == 0 and percentile(30, vals, 1) == 50
    assert percentile(10, vals, -1) == 100                                       # lower is better: cheapest wins
    assert usable("pe", -5) is None and usable("pe", 12) == 12 and usable("roe", -0.1) == -0.1


def test_scorecard_ranks_against_peers():
    me = row(year_change=0.5, pe=10, pb=1, roe=0.3, net_margin=0.25, revenue_growth=0.3, earnings_growth=0.4, beta=0.5, debt_to_equity=0.1)
    peers = [row(year_change=y, pe=p) for y, p in [(0.0, 15), (0.1, 20), (0.2, 25), (-0.1, 30)]]
    card = scorecard(me, peers)
    dims = {d["key"]: d for d in card["dimensions"]}
    assert all(d["score"] == 100 and d["tone"] == "good" for d in dims.values())
    assert dims["valuation"]["verdict"] == "Attractive" and dims["risk"]["verdict"] == "Low risk"
    assert card["medians"]["pe"] == 20 and card["peer_count"] == 4


def test_scorecard_skips_loss_makers_and_thin_data():
    me = row(pe=-8)                                                              # loss-making: P/E not ranked
    card = scorecard(me, [row(pe=12), row(pe=14), row(pe=16)])
    val = next(d for d in card["dimensions"] if d["key"] == "valuation")
    assert val["metrics"][0]["value"] == -8 and val["score"] is not None         # P/B still ranks
    thin = scorecard(row(), [row()])                                             # fewer than 4 values: no score
    assert all(d["score"] is None and d["verdict"] is None for d in thin["dimensions"])


def test_closest_by_size():
    pool = [("A", 1e9), ("B", 1e10), ("C", 1e11), ("D", None), ("E", 2e10)]
    assert closest_by_size(pool, 1.5e10, 3) == ["E", "B", "C"]                  # nearest on a log scale; unknown size last


XBRL = """<xbrl>
<in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="ShareholdingOfPromoterAndPromoterGroup_ContextI" decimals="4">0.5048</in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares>
<in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="InstitutionsForeign_ContextI" decimals="4">0.172</in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares>
<in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="MutualFundsOrUTI_ContextI" decimals="4">0.1011</in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares>
<in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="InstitutionsDomestic_ContextI" decimals="4">0.2119</in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares>
<in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="Governments_ContextI" decimals="4">0.001</in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares>
</xbrl>"""


def test_shareholding_xbrl():
    d = parse_shareholding_xbrl(XBRL)
    assert d == {"promoters": 50.48, "fii": 17.2, "mutual_funds": 10.11, "dii": 21.19, "government": 0.1,
                 "other_dii": 11.08, "retail_and_others": 11.03}
    no_promoter = parse_shareholding_xbrl(XBRL.replace("ShareholdingOfPromoterAndPromoterGroup_ContextI", "Unused_ContextI"))
    assert no_promoter["promoters"] == 0.0                                       # widely held (e.g. HDFC Bank)
    with pytest.raises(ValueError):
        parse_shareholding_xbrl("<xbrl></xbrl>")


def test_yahoo_fundamentals_parsing_and_roe_fallback():
    now = int(time.time())

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "fc.yahoo.com":
            return httpx.Response(404)
        if path.endswith("getcrumb"):
            return httpx.Response(200, text="CRUMB")
        if "quoteSummary" in path:
            v = lambda x: {"raw": x, "fmt": str(x)}  # noqa: E731
            return httpx.Response(200, json={"quoteSummary": {"error": None, "result": [{
                "summaryDetail": {"trailingPE": v(21.9), "marketCap": v(1.6e13), "dividendYield": v(0.0049), "beta": v(0.19)},
                "defaultKeyStatistics": {"priceToBook": v(1.8), "bookValue": v(668.0), "sharesOutstanding": v(1.35e10),
                                         "netIncomeToCommon": v(7.5e11), "trailingEps": v(55.2), "52WeekChange": v(-0.12)},
                "financialData": {"debtToEquity": v(46.3), "profitMargins": v(0.066), "revenueGrowth": v(0.297)},
                "assetProfile": {"sector": "Energy", "industry": "Oil & Gas Refining & Marketing"},
                "price": {"longName": "Reliance Industries Limited", "currency": "INR"}}]}})
        if "timeseries" in path:
            types = request.url.params["type"].split(",")
            assert "annualTotalRevenue" in types and "quarterlyNetIncome" in types
            return httpx.Response(200, json={"timeseries": {"result": [
                {"meta": {"type": ["annualTotalRevenue"]}, "annualTotalRevenue": [
                    {"asOfDate": "2025-03-31", "reportedValue": {"raw": 9.6e12}}, {"asOfDate": "2026-03-31", "reportedValue": {"raw": 1.05e13}}]},
                {"meta": {"type": ["quarterlyNetIncome"]}, "quarterlyNetIncome": [None, {"asOfDate": "2026-06-30", "reportedValue": {"raw": 2.09e11}}]},
            ]}})
        return httpx.Response(404)
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://query1.finance.yahoo.com")
    f = get_fundamentals("RELIANCE.NS", provider=YahooFinanceProvider(client=client), timeout_s=5).data
    assert f.pe == 21.9 and f.debt_to_equity == pytest.approx(0.463) and f.year_change == -0.12
    assert f.roe == pytest.approx(7.5e11 / (668.0 * 1.35e10))                    # computed: Yahoo left it blank
    assert [a.revenue for a in f.annual] == [9.6e12, 1.05e13] and f.quarterly[0].net_income == 2.09e11
    assert f.industry == "Oil & Gas Refining & Marketing" and now > 0


def test_research_endpoints_with_mock_data():
    from fastapi.testclient import TestClient

    from api import deps
    from api.main import app
    from api.options import get_nse
    from core.universes import Universes
    from api.live import get_live_provider
    from tools.providers.mock_provider import MockMarketDataProvider
    mock = MockMarketDataProvider()
    app.dependency_overrides.update({deps.get_universes: lambda: Universes.for_mock(["AAPL"]), get_nse: lambda: None,
                                     get_live_provider: lambda: mock})
    try:
        c = TestClient(app)
        assert c.get("/shareholding/AAPL").status_code == 404                    # not an Indian listing
        assert c.get("/fundamentals/not%20a%20symbol").status_code == 422
    finally:
        app.dependency_overrides.clear()
