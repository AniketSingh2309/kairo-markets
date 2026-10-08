"""Option chain (NSE): parsing, PCR / max pain, session renewal, contract series, and the API."""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.options import get_nse, market_open
from tools.providers.base import ProviderUnavailable
from tools.providers.nse import NseClient, contract_label, max_pain, parse_chain

EXP = "13-Oct-2026"


def side(strike, kind, ltp, oi):
    return {"identifier": f"OPTIDXNIFTY13-10-2026{kind}{strike:.2f}", "lastPrice": ltp, "change": -1.5, "pChange": -2.0,
            "openInterest": oi, "changeinOpenInterest": 10, "pchangeinOpenInterest": 5.0, "totalTradedVolume": 1000,
            "impliedVolatility": 9.5, "buyPrice1": ltp - 0.5, "buyQuantity1": 75, "sellPrice1": ltp + 0.5, "sellQuantity1": 150}


def chain_payload(spot=22603.05):
    strikes = [22500, 22550, 22600, 22650, 22700]
    ce_oi = {22500: 100, 22550: 200, 22600: 900, 22650: 400, 22700: 800}
    pe_oi = {22500: 700, 22550: 300, 22600: 600, 22650: 100, 22700: 50}
    rows = [{"strikePrice": k, "expiryDates": EXP, "CE": side(k, "CE", 150 - (k - 22500) / 4, ce_oi[k]),
             "PE": side(k, "PE", 60 + (k - 22500) / 4, pe_oi[k])} for k in strikes]
    rows.append({"strikePrice": 22600, "expiryDates": "19-Oct-2026", "CE": side(22600, "CE", 1, 1), "PE": None})
    return {"records": {"data": rows, "timestamp": "07-Oct-2026 15:40:00", "underlyingValue": spot,
                        "expiryDates": [EXP, "19-Oct-2026"], "strikePrices": strikes}, "filtered": {}}


def test_parse_chain():
    c = parse_chain(chain_payload(), "NIFTY", EXP)
    assert [r["strike"] for r in c["rows"]] == [22500, 22550, 22600, 22650, 22700]   # other expiries dropped
    assert c["atm"] == 22600 and c["spot"] == 22603.05 and c["observed_at"] == "2026-10-07T15:40:00+05:30"
    r = c["rows"][2]["ce"]
    assert (r["ltp"], r["oi"], r["iv"], r["bid"], r["ask"]) == (125.0, 900, 9.5, 124.5, 125.5)
    assert c["totals"]["ce_oi"] == 2400 and c["totals"]["pe_oi"] == 1750 and c["totals"]["pcr"] == pytest.approx(0.729, abs=1e-3)
    with pytest.raises(ProviderUnavailable):
        parse_chain({}, "NIFTY", EXP)


def test_max_pain_is_the_cheapest_expiry_for_writers():
    rows = parse_chain(chain_payload(), "NIFTY", EXP)["rows"]
    def payout(k):
        return sum(max(0, k - r["strike"]) * r["ce"]["oi"] + max(0, r["strike"] - k) * r["pe"]["oi"] for r in rows)
    assert max_pain(rows) == min((r["strike"] for r in rows), key=payout)
    assert max_pain([]) is None


def test_contract_label():
    assert contract_label("OPTIDXNIFTY13-10-2026CE22750.00") == "NIFTY 13 Oct 22750 CE"
    assert contract_label("OPTSTKBAJAJ-AUTO27-10-2026PE9000.00") == "BAJAJ-AUTO 27 Oct 9000 PE"
    assert contract_label("RELIANCE.NS") is None


def fake_nse(log, refuse_first=False):
    state = {"refused": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        log.append(path)
        if path == "/option-chain":
            return httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"})
        if refuse_first and not state["refused"]:
            state["refused"] = True
            return httpx.Response(403, text="denied", headers={"content-type": "text/html"})
        if path == "/api/underlying-information":
            return httpx.Response(200, json={"data": {"IndexList": [{"symbol": "NIFTY", "underlying": "Nifty 50"}],
                                                      "UnderlyingList": [{"symbol": "RELIANCE", "underlying": "Reliance Industries"}]}})
        if path == "/api/option-chain-contract-info":
            return httpx.Response(200, json={"expiryDates": [EXP, "19-Oct-2026"]})
        if path == "/api/option-chain-v3":
            assert request.url.params["type"] == ("Indices" if request.url.params["symbol"] == "NIFTY" else "Equity")
            return httpx.Response(200, json=chain_payload())
        if path == "/api/chart-databyindex":
            return httpx.Response(200, json={"closePrice": 274.9, "grapthData": [[1791364500000, 204.35], [1791364509000, 198.5], [1791364600000, 0]]})
        return httpx.Response(404)
    return NseClient(httpx.Client(transport=httpx.MockTransport(handler), base_url="https://www.nseindia.com"))


def test_client_renews_session_and_shifts_nse_times_to_utc():
    log: list[str] = []
    nse = fake_nse(log, refuse_first=True)
    c = nse.chain("NIFTY", None)
    assert c["expiry"] == EXP and log.count("/option-chain") == 2          # refused once -> new session, retried
    k = nse.contract("OPTIDXNIFTY13-10-2026CE22750.00")
    first = dt.datetime.fromtimestamp(k["ticks"][0][0], dt.timezone.utc)
    assert first == dt.datetime(2026, 10, 7, 3, 45, tzinfo=dt.timezone.utc)  # NSE's "09:15" is 09:15 IST = 03:45 UTC
    assert len(k["ticks"]) == 2 and k["label"] == "NIFTY 13 Oct 22750 CE"  # zero prices dropped


def test_market_hours():
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    assert market_open(dt.datetime(2026, 10, 7, 10, 0, tzinfo=ist))
    assert not market_open(dt.datetime(2026, 10, 7, 16, 0, tzinfo=ist))
    assert not market_open(dt.datetime(2026, 10, 10, 10, 0, tzinfo=ist))   # Saturday


def test_options_api():
    from api import deps
    from api.live import get_live_provider
    from core.store import Store
    from tools.providers.mock_provider import MockMarketDataProvider
    nse = fake_nse([])
    mock, store = MockMarketDataProvider(), Store(":memory:")
    app.dependency_overrides.update({get_nse: lambda: nse, get_live_provider: lambda: mock, deps.get_store: lambda: store})
    try:
        client = TestClient(app)
        u = client.get("/options/underlyings").json()
        assert u["indices"][0] == {"symbol": "NIFTY", "name": "Nifty 50", "type": "index", "yahoo": "^NSEI"}
        assert u["stocks"][0]["yahoo"] == "RELIANCE.NS"
        assert client.get("/options/for/%5ENSEI").json()["symbol"] == "NIFTY"
        assert client.get("/options/for/RELIANCE.NS").json()["symbol"] == "RELIANCE"
        assert client.get("/options/for/AAPL").status_code == 404
        c = client.get("/options/chain", params={"symbol": "nifty"}).json()
        assert c["symbol"] == "NIFTY" and c["expiry"] == EXP and c["yahoo"] == "^NSEI" and len(c["rows"]) == 5
        assert client.get("/options/chain", params={"symbol": "NIFTY", "expiry": "2026-10-13"}).status_code == 422
        assert client.get("/options/chain", params={"symbol": "../etc"}).status_code == 422
        k = client.get("/options/contract", params={"id": "OPTIDXNIFTY13-10-2026CE22750.00"}).json()
        assert len(k["t"]) == len(k["p"]) == 2 and k["prev_close"] == 274.9
        assert client.get("/options/contract", params={"id": "RELIANCE"}).status_code == 422
    finally:
        app.dependency_overrides.clear()


# --- phase 2: Greeks, build-up, straddles, PCR history ----------------------------------------------------

import math  # noqa: E402

from core import options_math as om  # noqa: E402


def test_black_scholes_parity_and_greeks():
    S, K, T, sig = 22600.0, 22600.0, 6 / 365, 0.12
    c, p = om.bs_price("CE", S, K, T, sig), om.bs_price("PE", S, K, T, sig)
    assert c - p == pytest.approx(S - K * math.exp(-om.DEFAULT_RATE * T), abs=1e-6)       # put-call parity
    gc, gp = om.greeks("CE", S, K, T, sig), om.greeks("PE", S, K, T, sig)
    assert gc["delta"] - gp["delta"] == pytest.approx(1, abs=1e-3) and 0.45 < gc["delta"] < 0.6
    assert gc["gamma"] == pytest.approx(gp["gamma"]) and gc["vega"] == pytest.approx(gp["vega"])
    assert gc["theta"] < 0 and gp["theta"] < 0                                           # long options lose value daily


def test_implied_vol_round_trip_and_stale_prices():
    S, K, T = 22600.0, 22300.0, 6 / 365
    price = om.bs_price("CE", S, K, T, 0.18)
    assert om.implied_vol("CE", price, S, K, T) == pytest.approx(0.18, abs=1e-4)
    assert om.implied_vol("CE", 250, S, K, T) is None                                    # below intrinsic: a stale trade
    g = om.side_greeks("CE", {"ltp": price, "iv": 0}, S, K, T)
    assert g["iv_source"] == "price" and g["iv"] == pytest.approx(18, abs=0.01)
    assert om.side_greeks("CE", {"ltp": 250, "iv": 0}, S, K, T) is None


def test_buildup_labels():
    assert om.buildup(5, 100)["label"] == "Long build-up" and om.buildup(-5, 100)["label"] == "Short build-up"
    assert om.buildup(5, -100)["label"] == "Short covering" and om.buildup(-5, -100)["label"] == "Long unwinding"
    assert om.buildup(0, 100) is None and om.buildup(None, 5) is None


def test_years_to_expiry_counts_to_the_close():
    now = dt.datetime(2026, 10, 13, 15, 0, tzinfo=om.IST)
    assert om.years_to_expiry(dt.date(2026, 10, 13), now) == pytest.approx(30 * 60 / (365 * 86400))
    assert om.years_to_expiry(dt.date(2026, 10, 12), now) == om.MIN_T


def test_enrich_adds_greeks_without_touching_the_cache():
    from api.options import enrich
    data = parse_chain(chain_payload(), "NIFTY", EXP)
    before = repr(data)
    out = enrich(data, dt.datetime(2026, 10, 7, 15, 40, tzinfo=om.IST))
    assert repr(data) == before                                                           # cached chain unchanged
    atm = next(r for r in out["rows"] if r["strike"] == 22600)
    assert 0.4 < atm["ce"]["greeks"]["delta"] < 0.7 and atm["pe"]["greeks"]["delta"] < 0
    assert atm["ce"]["buildup"]["label"] == "Short build-up"                              # price -1.5, OI +10
    assert out["days_to_expiry"] == pytest.approx(5.99, abs=0.01)


def test_merge_legs_per_minute_carries_prices_forward():
    from api.options import merge_legs
    ce = [(1000, 10.0), (1010, 11.0), (1130, 12.0)]
    pe = [(1005, 20.0), (1200, 19.0)]
    assert merge_legs(ce, pe) == [(960, 11.0, 20.0), (1080, 12.0, 20.0), (1200, 12.0, 19.0)]


def test_straddle_and_oi_history_endpoints():
    from api import deps
    from api.live import get_live_provider
    from api.options import RECORDER
    from core.store import Store
    from tools.providers.mock_provider import MockMarketDataProvider
    nse, store = fake_nse([]), Store(":memory:")
    app.dependency_overrides.update({get_nse: lambda: nse, get_live_provider: lambda: MockMarketDataProvider(), deps.get_store: lambda: store})
    RECORDER.ensure_running = lambda *a: None
    try:
        c = TestClient(app)
        s = c.get("/options/straddle", params={"symbol": "NIFTY"}).json()
        assert s["strike"] == 22600 and s["straddle"] == [round(a + b, 2) for a, b in zip(s["ce"], s["pe"])]
        assert s["breakevens"] == [round(22600 - s["premium"], 2), round(22600 + s["premium"], 2)]
        assert c.get("/options/straddle", params={"symbol": "NIFTY", "strike": 1}).status_code == 404
        store.add_oi_snapshot({"symbol": "NIFTY", "expiry": EXP, "at": dt.datetime.now(dt.timezone.utc).isoformat(), "pcr": 0.8, "spot": 22600})
        h = c.get("/options/oi-history", params={"symbol": "NIFTY", "expiry": EXP}).json()
        assert len(h["points"]) == 1 and h["points"][0]["pcr"] == 0.8
    finally:
        app.dependency_overrides.clear()


# --- strategies ----------------------------------------------------------------------------------------

from core import strategy as stg  # noqa: E402


def test_iron_condor_numbers():
    ic = [stg.Leg("PE", "BUY", 22100, 65, 20), stg.Leg("PE", "SELL", 22300, 65, 55),
          stg.Leg("CE", "SELL", 22700, 65, 50), stg.Leg("CE", "BUY", 22900, 65, 18)]
    a = stg.analyse(ic, 22500, 5 / 365, 0.13)
    assert a["net_premium"] == 4355 and a["max_profit"] == 4355 and a["max_loss"] == -8645
    assert a["breakevens"] == [22233.0, 22767.0] and 40 < a["pop"] < 75
    assert stg.short_option_margin(ic) == pytest.approx(0.15 * (22300 + 22700) * 65)


def test_unlimited_sides_and_breakevens():
    long_call = [stg.Leg("CE", "BUY", 22500, 65, 120)]
    a = stg.analyse(long_call, 22500, 5 / 365, 0.13)
    assert a["max_profit"] is None and a["max_loss"] == -7800 and a["breakevens"] == [22620.0]
    short_straddle = [stg.Leg("CE", "SELL", 22500, 65, 120), stg.Leg("PE", "SELL", 22500, 65, 148)]
    b = stg.analyse(short_straddle, 22500, 5 / 365, 0.13)
    assert b["max_loss"] is None and b["max_profit"] == 17420 and b["breakevens"] == [22232.0, 22768.0]
    assert b["greeks"]["theta"] > 0 and b["greeks"]["vega"] < 0                              # short premium earns time decay
    with pytest.raises(ValueError):
        stg.analyse([], 22500, 0.01, 0.13)


def test_strategy_endpoint():
    from api import deps
    from api.live import get_live_provider
    from core.store import Store
    from tools.providers.mock_provider import MockMarketDataProvider
    nse = fake_nse([])
    nse.lot_size = lambda symbol, expiry: 65
    app.dependency_overrides.update({get_nse: lambda: nse, get_live_provider: lambda: MockMarketDataProvider(),
                                     deps.get_store: lambda: Store(":memory:")})
    try:
        c = TestClient(app)
        r = c.post("/options/strategy", json={"symbol": "NIFTY", "legs": [{"kind": "CE", "side": "BUY", "strike": 22600, "lots": 2},
                                                                          {"kind": "CE", "side": "SELL", "strike": 22700, "lots": 2}]}).json()
        assert r["lot_size"] == 65 and [leg["qty"] for leg in r["legs"]] == [130, 130]
        assert r["net_premium"] == pytest.approx(-(125 - 100) * 130) and r["max_profit"] == pytest.approx((100 - 25) * 130)
        assert r["legs"][0]["symbol"].startswith("OPT:OPTIDXNIFTY") and r["margin"] == pytest.approx(0.15 * 22700 * 130)
        bad = c.post("/options/strategy", json={"symbol": "NIFTY", "legs": [{"kind": "CE", "side": "BUY", "strike": 1}]})
        assert bad.status_code == 422
    finally:
        app.dependency_overrides.clear()
