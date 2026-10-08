"""Portfolio, tax, health, news, alerts and screener endpoints (mock data, in-memory DB)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time

import pytest
from fastapi.testclient import TestClient

from api import deps
from api.live import get_live_provider, get_stream_hub
from api.main import app
from core.alerts import AlertEngine, evaluate
from core.health import HoldingInput, build_health
from core.screener import screen_row
from core.store import Store
from core.universes import Universes
from tests.test_forecast import make_bars, random_walk


@pytest.fixture
def env(mock_provider):
    store = Store(":memory:")
    engine = AlertEngine(store, mock_provider, None, timeout_s=2, poll_without_stream_s=0)
    universes = Universes.for_mock(mock_provider.symbols)
    for cache in (deps.QUOTES, deps.HISTORY, deps.PROFILES, deps.NEWS):
        cache._data.clear()
    app.dependency_overrides.update({
        deps.get_store: lambda: store, get_live_provider: lambda: mock_provider,
        deps.get_alert_engine: lambda: engine, get_stream_hub: lambda: None,
        deps.get_universes: lambda: universes,
    })
    with TestClient(app) as client:
        yield client, store, engine
    app.dependency_overrides.clear()


def add(client, symbol, side, qty, price, day, fees=0):
    return client.post("/portfolio/transactions", json={
        "symbol": symbol, "side": side, "quantity": qty, "price": price, "fees": fees, "trade_date": day})


# --- portfolio ---------------------------------------------------------------------------------


def test_portfolio_lifecycle(env):
    client, store, _ = env
    assert add(client, "AAPL", "buy", 10, 200, "2025-01-15", fees=5).status_code == 201
    assert add(client, "MSFT", "buy", 2, 400, "2025-02-01").status_code == 201
    r = add(client, "ZZZZ", "buy", 1, 1, "2025-02-01")
    assert r.status_code == 404 and "Unknown symbol" in r.json()["detail"]
    assert add(client, "AAPL", "buy", 0, 1, "2025-01-01").status_code == 422

    body = client.get("/portfolio").json()
    aapl = next(p for p in body["positions"] if p["symbol"] == "AAPL")
    assert aapl["price"] == 246.1 and aapl["currency"] == "USD"
    assert aapl["invested"] == pytest.approx(2005) and aapl["value"] == pytest.approx(2461)
    assert aapl["xirr"] is not None and aapl["name"] == "Apple Inc. (mock)"
    usd = body["groups"][0]
    assert usd["currency"] == "USD" and usd["positions"] == 2 and usd["xirr"] is not None

    txns = client.get("/portfolio/transactions").json()
    assert len(txns) == 2 and txns[0]["symbol"] == "MSFT"  # newest first
    assert client.delete(f"/portfolio/transactions/{txns[0]['id']}").status_code == 204
    assert client.delete("/portfolio/transactions").status_code == 400
    assert client.delete("/portfolio/transactions?confirm=true").json() == {"deleted": 1}


def test_csv_import_dry_run_then_commit(env):
    client, store, _ = env
    csv = "date,symbol,side,quantity,price\n2025-01-10,AAPL,buy,3,180\n2025-01-11,ZZZZ,buy,1,5\nnope,MSFT,buy,1,1\n"
    preview = client.post("/portfolio/import", json={"csv": csv, "dry_run": True}).json()
    assert preview["imported"] == 0 and [r["symbol"] for r in preview["rows"]] == ["AAPL"]
    assert {e["message"].split(" ")[0] for e in preview["errors"]} >= {"unknown", "unrecognised"}
    assert store.list_transactions() == []
    done = client.post("/portfolio/import", json={"csv": csv}).json()
    assert done["imported"] == 1 and store.list_transactions()[0]["source"] == "import:generic"


def test_tax_endpoint(env):
    client, _, _ = env
    add(client, "AAPL", "buy", 1, 100, "2025-05-01")
    add(client, "AAPL", "sell", 1, 150, "2025-06-01")
    body = client.get("/portfolio/tax?fy=FY 2025-26").json()
    assert body["report"]["foreign"][0]["stcg"] == 50
    assert "FY 2025-26" in body["available_fys"]
    assert client.get("/portfolio/tax?fy=2025").status_code == 422


def test_health_endpoint(env):
    client, _, _ = env
    add(client, "RWLK", "buy", 100, 40, "2025-01-10")
    add(client, "AAPL", "buy", 1, 200, "2025-01-10")
    body = client.get("/portfolio/health?currency=USD").json()
    assert 0 <= body["score"] <= 100 and body["grade"] in "ABCD"
    ids = {c["id"] for c in body["checks"]}
    assert {"holdings", "concentration", "sector"} <= ids
    assert client.get("/portfolio/health?currency=INR").status_code == 404


# --- health (unit) --------------------------------------------------------------------------------


def test_health_flags_concentration_and_scores_traceably():
    bars = {s: make_bars(random_walk(300, seed)) for s, seed in (("A", 1), ("B", 2))}
    rep = build_health("INR", [HoldingInput(symbol="A", value=900, sector="Energy"),
                               HoldingInput(symbol="B", value=100, sector="Energy", pnl_pct=-40)],
                       bars, ("Bench", make_bars(random_walk(300, 9))))
    by_id = {c.id: c for c in rep.checks}
    assert by_id["holdings"].status == "bad" and by_id["concentration"].status == "bad"
    assert by_id["sector"].status == "bad" and "losers" in by_id
    assert rep.score == 100 - sum({"bad": 20, "warn": 8}.get(c.status, 0) for c in rep.checks)
    assert rep.metrics.volatility_pct and rep.metrics.beta is not None and rep.metrics.days_used >= 250


# --- news ------------------------------------------------------------------------------------------


def test_news_merges_and_sorts(env):
    client, _, _ = env
    body = client.get("/news?symbols=AAPL,MSFT,ZZZZ&period=90d").json()
    times = [i["published_at"] for i in body["items"]]
    assert times == sorted(times, reverse=True) and len(times) == 5
    assert {s for i in body["items"] for s in i["symbols"]} == {"AAPL", "MSFT"}
    assert body["errors"] == {"ZZZZ": "UNKNOWN_SYMBOL"}
    assert client.get("/news?symbols=").status_code == 422


# --- alerts ----------------------------------------------------------------------------------------


def test_alert_condition_logic():
    assert evaluate("price_above", 100, 100.0, None) == 100
    assert evaluate("price_below", 100, 100.5, None) is None
    assert evaluate("day_change_below", -2, 50, -2.5) == -2.5
    assert evaluate("day_change_above", 3, 50, None) is None


def test_alert_api_and_engine_fire_once(env, monkeypatch):
    client, store, engine = env
    monkeypatch.setattr(engine, "ensure_running", lambda: None)  # drive the engine cycle by hand
    assert client.post("/alerts", json={"symbol": "AAPL", "kind": "price_above", "threshold": -1}).status_code == 422
    assert client.post("/alerts", json={"symbol": "ZZZZ", "kind": "price_above", "threshold": 1}).status_code == 404
    hit = client.post("/alerts", json={"symbol": "AAPL", "kind": "price_above", "threshold": 200}).json()
    miss = client.post("/alerts", json={"symbol": "AAPL", "kind": "price_below", "threshold": 100}).json()
    assert hit["status"] == "active"

    async def cycle():
        q = engine.listen()
        await engine.run_once()
        return [q.get_nowait() for _ in range(q.qsize())]

    events = asyncio.run(cycle())
    assert len(events) == 1 and events[0]["alert"]["id"] == hit["id"] and "246.10" in events[0]["message"]
    assert asyncio.run(cycle()) == []  # fires once
    states = {a["id"]: a["status"] for a in client.get("/alerts").json()["alerts"]}
    assert states == {hit["id"]: "triggered", miss["id"]: "active"}
    assert client.post(f"/alerts/{hit['id']}/rearm").json()["status"] == "active"
    assert client.delete(f"/alerts/{miss['id']}").status_code == 204
    assert client.delete(f"/alerts/{miss['id']}").status_code == 404


def test_alert_stream_delivers_triggers(env):
    client, store, engine = env
    store.add_alert("MSFT", "price_above", 1)
    with client.stream("GET", "/alerts/stream?max_events=1") as r:
        lines = [l for l in r.iter_lines() if l.startswith("data:")]
    assert json.loads(lines[0][5:])["alert"]["symbol"] == "MSFT"


# --- screener -------------------------------------------------------------------------------------


def test_screen_row_metrics():
    bars = make_bars([100 * 1.001 ** i for i in range(300)])
    row = screen_row("UP", "Uptrend", "INR", bars)
    assert row.chg_1y > 0 and row.above_sma50 and row.above_sma200 and row.golden_cross
    assert row.from_52w_high <= 0 <= row.from_52w_low and row.rsi14 > 70


def _poll_screen(client, url, timeout=15):
    deadline = time.time() + timeout
    while True:
        body = client.get(url).json()
        if body["status"] == "ready" or time.time() > deadline:
            return body
        time.sleep(0.1)


def test_screener_job_completes(env):
    client, _, _ = env
    cat = client.get("/screener/universes").json()
    assert cat["default"] == "mock" and [u["key"] for u in cat["items"]] == ["mock"]
    first = _poll_screen(client, "/screener")  # no universe given: the default one
    assert first["key"] == "mock" and first["total"] == len(first["rows"]) + len(first["errors"])
    deadline = time.time() + 15
    while True:
        body = client.get("/screener?symbols=AAPL,MSFT,RWLK,ZZZZ").json()
        if body["status"] == "ready" or time.time() > deadline:
            break
        time.sleep(0.1)
    assert body["status"] == "ready" and body["done"] == 4
    assert {r["symbol"] for r in body["rows"]} == {"AAPL", "MSFT", "RWLK"}
    assert body["errors"] == {"ZZZZ": "UNKNOWN_SYMBOL"}
    assert client.get("/screener?universe=nope").status_code == 404


def test_alert_endpoints_start_engine_on_event_loop(env):
    client, store, engine = env
    store.add_alert("AAPL", "price_below", 1)
    assert client.get("/alerts").status_code == 200   # used to 500: engine started from a worker thread
    alert_id = store.list_alerts()[0]["id"]
    assert client.post(f"/alerts/{alert_id}/rearm").status_code == 200
    assert client.delete(f"/alerts/{alert_id}").status_code == 204


def test_app_files_are_revalidated(env):
    client, _, _ = env
    for path in ("/", "/static/js/app.js", "/static/css/app.css"):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"


def test_index_versions_assets_and_modules(env):
    client, _, _ = env
    html = client.get("/").text
    assert '/static/css/app.css?v=' in html and '/static/js/app.js?v=' in html
    assert '<script type="importmap">' in html and '"/static/js/core.js": "/static/js/core.js?v=' in html
