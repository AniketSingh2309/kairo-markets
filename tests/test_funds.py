"""Phase 2: mutual funds -- AMFI parsing, classification, analytics, API, portfolio and tax."""

from __future__ import annotations

import datetime as dt
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from core import mutual_funds as mf
from core.portfolio import Transaction, build_book
from core.tax_india import category_of, fy_report
from tests.test_platform_api import env  # noqa: F401 - shared fixture
from tools.models import is_mf_symbol, mf_symbol
from tools.providers.amfi_provider import AmfiProvider, CompositeProvider, parse_history_report, parse_navall

TODAY = dt.date(2026, 10, 5)

NAVALL = """Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date

Open Ended Schemes(Equity Scheme - Flexi Cap Fund)

Alpha Mutual Fund

100001;INF000A01;-;Alpha Flexi Cap Fund;Direct Plan;Growth Option;160.0000;05-Oct-2026
100002;INF000A02;-;Alpha Flexi Cap Fund;Regular Plan;Growth Option;150.0000;05-Oct-2026
100003;INF000A03;-;Beta Flexi Cap Fund;Direct Plan;Growth Option;133.1000;05-Oct-2026
100004;INF000A04;-;Gamma Flexi Cap Fund;Direct Plan;Growth Option;120.0000;05-Oct-2026
100005;INF000A05;-;Delta Flexi Cap Fund;Direct Plan;Growth Option;105.0000;05-Oct-2026
100006;INF000A06;-;Delta Flexi Cap Fund;Direct Plan;IDCW Option;20.0000;05-Oct-2026

Open Ended Schemes(Debt Scheme - Liquid Fund)

Alpha Mutual Fund

200001;INF000B01;-;Alpha Liquid Fund;Direct Plan;Growth Option;1100.0000;05-Oct-2026

Open Ended Schemes(Other Scheme - Index Funds)

Beta Mutual Fund

300001;INF000C01;-;Beta Nifty 50 Index Fund;Direct Plan;Growth Option;50.0000;05-Oct-2026
300002;INF000C02;-;Beta Nasdaq 100 FoF;Direct Plan;Growth Option;40.0000;05-Oct-2026

Close Ended Schemes(Income)

Old Mutual Fund

400001;INF000D01;-;Old FMP Series 1;;;10.0000;05-Oct-2019
"""

# Growth per year for each scheme, used both for snapshots and for the NAV-history mirror.
GROWTH = {"100001": 0.12, "100002": 0.11, "100003": 0.10, "100004": 0.06, "100005": 0.02, "100006": 0.0,
          "200001": 0.065, "300001": 0.09, "300002": 0.15}


def nav_at(code: str, day: dt.date) -> float:
    latest = {"100001": 160.0, "100002": 150.0, "100003": 133.1, "100004": 120.0, "100005": 105.0, "100006": 20.0,
              "200001": 1100.0, "300001": 50.0, "300002": 40.0}[code]
    years = (TODAY - day).days / 365.25
    return latest / (1 + GROWTH[code]) ** years


def fake_amfi(request: httpx.Request) -> httpx.Response:
    url = urlparse(str(request.url))
    if url.path.endswith("NAVAll.txt"):
        return httpx.Response(200, text=NAVALL)
    if "DownloadNAVHistoryReport" in url.path:
        day = dt.datetime.strptime(parse_qs(url.query)["frmdt"][0], "%d-%b-%Y").date()
        rows = [f"{c};Scheme {c};;;;;{nav_at(c, day):.4f};{day:%d-%b-%Y}" for c in GROWTH] * 1
        filler = [f"9{i:05d};Filler;;;;;10.0;{day:%d-%b-%Y}" for i in range(1200)]  # a realistic-sized report
        return httpx.Response(200, text="Scheme Code;NAV Name;Plan;Option;ISIN;ISIN2;Net Asset Value;Date\n\n" + "\n".join(rows + filler))
    if url.path.startswith("/mf/"):
        code = url.path.rsplit("/", 1)[-1]
        if code not in GROWTH:
            return httpx.Response(404, json={})
        days = [TODAY - dt.timedelta(days=i) for i in range(0, 6 * 365) if (TODAY - dt.timedelta(days=i)).weekday() < 5]
        data = [{"date": d.strftime("%d-%m-%Y"), "nav": f"{nav_at(code, d):.4f}"} for d in days]
        return httpx.Response(200, json={"meta": {"scheme_code": int(code)}, "data": data})
    return httpx.Response(404)


@pytest.fixture
def amfi():
    return AmfiProvider(client=httpx.Client(transport=httpx.MockTransport(fake_amfi), base_url="https://test"))


# --- parsing & classification -------------------------------------------------------------------


def test_parse_navall_current_and_legacy_layouts():
    schemes = parse_navall(NAVALL)
    s = schemes["100001"]
    assert (s.name, s.amc, s.category, s.plan, s.option, s.nav, s.nav_date) == (
        "Alpha Flexi Cap Fund", "Alpha Mutual Fund", "Equity Scheme - Flexi Cap Fund", "Direct", "Growth", 160.0, TODAY)
    assert schemes["100006"].option == "IDCW" and schemes["400001"].scheme_type == "Close Ended Schemes"
    legacy = ("Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date\n\n"
              "Open Ended Schemes(Equity Scheme - Large Cap Fund)\n\nX Mutual Fund\n\n"
              "555;INF1;-;X Bluechip Fund - Direct Plan - Growth;45.67;03-Oct-2026\n")
    old = parse_navall(legacy)["555"]
    assert (old.nav, old.plan, old.option, old.category) == (45.67, "Direct", "Growth", "Equity Scheme - Large Cap Fund")


def test_parse_history_report():
    text = "Scheme Code;NAV Name;Plan;Option;ISIN;ISIN2;Net Asset Value;Date\n\nOpen Ended Schemes ( Liquid )\n\nX MF\n\n11;A;;;;;12.5;03-Oct-2025\n12;B;;;;;N.A.;03-Oct-2025\n"
    assert parse_history_report(text) == {"11": 12.5}


@pytest.mark.parametrize("category,name,asset,kind", [
    ("Equity Scheme - Flexi Cap Fund", "X Flexi Cap", "Equity", "equity"),
    ("Debt Scheme - Liquid Fund", "X Liquid", "Debt", "debt"),
    ("Hybrid Scheme - Arbitrage Fund", "X Arbitrage", "Hybrid", "equity"),
    ("Hybrid Scheme - Conservative Hybrid Fund", "X Conservative", "Hybrid", "debt"),
    ("Hybrid Scheme - Multi Asset Allocation", "X Multi Asset", "Hybrid", "other"),
    ("Other Scheme - Index Funds", "X Nifty 50 Index Fund", "Index & ETF", "equity"),
    ("Other Scheme - Index Funds", "X Nasdaq 100 Index Fund", "Index & ETF", "other"),
    ("Other Scheme - Index Funds", "X CRISIL IBX Gilt Index", "Index & ETF", "debt"),
    ("Other Scheme - Gold ETF", "X Gold ETF", "Index & ETF", "other"),
])
def test_classification(category, name, asset, kind):
    for cat in (category, mf.canonical_category(category, name)):
        c = mf.classify(cat, name)
        assert (c.asset_class, c.tax_kind) == (asset, kind)


@pytest.mark.parametrize("raw,name,canonical", [
    ("Equity Scheme - ELSS", "X Tax Saver", "Equity Scheme - ELSS (Tax Saver)"),
    ("Equity Schemes - ELSS- Tax Saver Fund", "X ELSS", "Equity Scheme - ELSS (Tax Saver)"),
    ("Equity Schemes - Flexi Cap Fund", "X Flexi", "Equity Scheme - Flexi Cap Fund"),
    ("Equity Scheme - Sectoral/ Thematic", "X Pharma", "Equity Scheme - Sectoral/Thematic"),
    ("Equity Schemes - Thematic Fund", "X Manufacturing", "Equity Scheme - Sectoral/Thematic"),
    ("Income/Debt Oriented Schemes - Banking and PSU Debt Fund", "X BPSU", "Debt Scheme - Banking and PSU Fund"),
    ("Debt Scheme - Banking and PSU Fund", "X BPSU", "Debt Scheme - Banking and PSU Fund"),
    ("Income/Debt Oriented Schemes - Short Term Fund", "X ST", "Debt Scheme - Short Duration Fund"),
    ("Hybrid Schemes - Balanced Advantage Fund/ Dynamic Asset Allocation", "X BAF",
     "Hybrid Scheme - Balanced Advantage / Dynamic Asset Allocation"),
    ("Hybrid Scheme - Dynamic Asset Allocation or Balanced Advantage", "X BAF",
     "Hybrid Scheme - Balanced Advantage / Dynamic Asset Allocation"),
    ("Other Scheme - Index Funds", "X Nifty 50 Index Fund", "Other Scheme - Index Funds (Equity)"),
    ("Index Funds - Debt Funds", "X Target Maturity", "Other Scheme - Index Funds (Debt)"),
    ("Index Funds - Equity Funds", "X Nasdaq 100 Index", "Other Scheme - Index Funds (International & other)"),
    ("Fund of Funds Scheme (Domestic)", "X FoF", "Other Scheme - FoF Domestic"),
    ("Solution Oriented Schemes ** - Retirement Fund", "X Retirement", "Solution Oriented Scheme - Retirement Fund"),
    (None, "X", None),
])
def test_canonical_category_merges_amfi_naming_variants(raw, name, canonical):
    assert mf.canonical_category(raw, name) == canonical


def test_index_debt_bucket_is_taxed_as_debt_even_without_debt_words_in_name():
    assert mf.classify("Other Scheme - Index Funds (Debt)", "X Nifty SDL Apr 2027").tax_kind == "debt"
    assert mf.classify("Other Scheme - Index Funds (Debt)", "X Target Apr 2027").tax_kind == "debt"


# --- analytics -------------------------------------------------------------------------------------


def steady(rate: float, years: int = 6, end=TODAY, start_nav=10.0):
    days = [end - dt.timedelta(days=i) for i in range(years * 365, -1, -1)]
    return [(d, start_nav * (1 + rate) ** ((d - days[0]).days / 365.25)) for d in days]


def test_returns_cagr_and_rolling_on_steady_growth():
    s = steady(0.10)
    r = mf.returns(s)
    assert r.periods["1Y"] == pytest.approx(10, abs=0.1) and r.periods["3Y"] == pytest.approx(10, abs=0.1)
    assert r.periods["10Y"] is None and r.since_inception_cagr == pytest.approx(10, abs=0.1)
    roll = mf.rolling(s, 3)
    assert roll.pct_positive == 100 and roll.median == pytest.approx(10, abs=0.2)


def test_risk_finds_drawdown_and_recovery():
    s = [(TODAY - dt.timedelta(days=400 - i), v) for i, v in enumerate([100] * 100 + [70] * 100 + [110] * 201)]
    r = mf.risk(s)
    assert r.max_drawdown_pct == pytest.approx(-30) and r.recovered_on is not None and r.current_drawdown_pct == 0


def test_sip_backtest_flat_nav_and_step_up():
    flat = [(TODAY - dt.timedelta(days=i), 10.0) for i in range(800, -1, -1)]
    r = mf.sip_backtest(flat, 1000, 2)
    assert r.instalments == 25 and r.invested == r.value == 25000 and abs(r.xirr_pct) < 0.01
    up = mf.sip_backtest(flat, 1000, 2, step_up_pct=10)
    assert up.invested == pytest.approx(12 * 1000 + 12 * 1100 + 1 * 1210)


def test_rank_category():
    r = mf.rank_category({"a": 12, "b": 10, "c": 8, "d": 2}, "b", "3Y")
    assert (r.rank, r.of, r.quartile) == (2, 4, 2) and r.percentile == pytest.approx(66.7, abs=0.1)
    assert mf.rank_category({"a": 1, "b": 2}, "a", "1Y") is None


# --- provider + API ---------------------------------------------------------------------------------


@pytest.fixture
def fund_client(env, amfi, mock_provider):  # noqa: F811
    from api import funds as funds_api
    from api.live import get_live_provider
    from api.main import app

    client, store, engine = env
    funds_api.RANKINGS.at, funds_api.RANKINGS.returns = 0.0, {}
    app.dependency_overrides[get_live_provider] = lambda: CompositeProvider(mock_provider, amfi)
    yield client, store


def test_mf_symbols():
    assert is_mf_symbol("MF122639") and not is_mf_symbol("MFX") and mf_symbol(122639) == "MF122639"


def test_fund_endpoints(fund_client):
    client, _ = fund_client
    cats = {c["category"]: c for c in client.get("/funds/categories").json()}
    assert cats["Equity Scheme - Flexi Cap Fund"]["funds"] == 4 and "Income" not in cats  # matured scheme dropped
    found = client.get("/funds/search", params={"q": "flexi"}).json()
    assert {i["code"] for i in found["items"]} == {"100001", "100003", "100004", "100005"}  # direct growth only
    top = client.get("/funds/top", params={"category": "Equity Scheme - Flexi Cap Fund", "period": "3Y"}).json()
    assert [i["code"] for i in top["items"]] == ["100001", "100003", "100004", "100005"]
    assert top["items"][0]["returns"]["3Y"] == pytest.approx(12, abs=0.3)
    d = client.get("/funds/MF100003").json()
    assert d["classification"]["tax_kind"] == "equity" and d["returns"]["periods"]["3Y"] == pytest.approx(10, abs=0.3)
    ranks = {r["period"]: r for r in d["ranks"]}
    assert ranks["3Y"]["rank"] == 2 and ranks["3Y"]["of"] == 4
    sip = client.get("/funds/100003/sip", params={"amount": 5000, "years": 3}).json()
    assert sip["xirr_pct"] == pytest.approx(10, abs=0.6) and sip["value"] > sip["invested"]
    cmp = client.get("/funds/compare", params={"codes": "100001,300001"}).json()
    assert len(cmp["funds"]) == 2 and cmp["funds"][0]["growth"][0][1] == 100
    assert client.get("/funds/999999").status_code == 404
    assert client.get("/funds/compare", params={"codes": "100001"}).status_code == 422


def test_mutual_fund_in_portfolio_and_tax(fund_client):
    client, store = fund_client
    assert client.post("/portfolio/transactions", json={"symbol": "MF100003", "side": "buy", "quantity": 100,
                                                        "price": 100, "trade_date": "2024-01-10"}).status_code == 201
    body = client.get("/portfolio").json()
    pos = body["positions"][0]
    assert pos["name"] == "Beta Flexi Cap Fund" and pos["price"] == pytest.approx(133.1) and pos["currency"] == "INR"
    # Debt fund bought after 1-Apr-2023 and sold: slab-rate gains, not equity buckets.
    client.post("/portfolio/transactions", json={"symbol": "MF200001", "side": "buy", "quantity": 10, "price": 1000, "trade_date": "2025-05-02"})
    client.post("/portfolio/transactions", json={"symbol": "MF200001", "side": "sell", "quantity": 10, "price": 1050, "trade_date": "2025-09-01"})
    tax = client.get("/portfolio/tax", params={"fy": "FY 2025-26"}).json()["report"]
    assert tax["mutual_funds"]["debt_slab_gains"] == pytest.approx(500) and tax["indian"] is None


def test_mf_tax_categories():
    kinds = {"MF101": "equity", "MF102": "debt", "MF103": "other"}.get
    assert category_of("MF101", None, kinds) == "equity_mf"
    assert category_of("MF102", dt.date(2023, 4, 1), kinds) == "debt_mf"
    assert category_of("MF102", dt.date(2023, 3, 31), kinds) == "other_mf"  # pre-50AA purchase
    book = build_book([
        Transaction(id=1, symbol="MF101", side="buy", quantity=10, price=100, trade_date=dt.date(2023, 1, 1), currency="INR"),
        Transaction(id=2, symbol="MF101", side="sell", quantity=10, price=150, trade_date=dt.date(2025, 6, 1), currency="INR"),
        Transaction(id=3, symbol="MF103", side="buy", quantity=10, price=100, trade_date=dt.date(2022, 1, 1), currency="INR"),
        Transaction(id=4, symbol="MF103", side="sell", quantity=10, price=200, trade_date=dt.date(2025, 6, 1), currency="INR"),
    ])
    rep = fy_report(book, "FY 2025-26", kinds)
    assert rep.indian.ltcg == 500  # equity fund taxed with shares
    assert rep.mutual_funds.other_long_gains == 1000 and rep.mutual_funds.other_long_tax == pytest.approx(1000 * 0.125 * 1.04)
