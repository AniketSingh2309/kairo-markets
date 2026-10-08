from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agents.orchestrator import Orchestrator
from api.main import app, get_orchestrator


@pytest.fixture
def client(settings, clock, mock_provider):
    app.dependency_overrides[get_orchestrator] = lambda: Orchestrator(
        settings, provider=mock_provider, clock=clock)
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_analyze_ok(client):
    r = client.post("/analyze", json={"symbol": "AAPL", "question": "How has it trended?"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["answer"]["kind"] == "generated_interpretation"
    assert body["reported_facts"][0]["observed_at"].startswith("2026-09-29T20:00")
    assert body["sources"] and body["trace"] and body["tool_calls"]


def test_analyze_without_trace(client):
    r = client.post("/analyze?include_trace=false", json={"symbol": "AAPL", "question": "Trend?"})
    assert r.json()["trace"] == [] and r.json()["tool_calls"] == []


def test_unknown_symbol_is_404(client):
    r = client.post("/analyze", json={"symbol": "ZZZZ", "question": "How is it doing?"})
    assert r.status_code == 404
    assert r.json()["flags"][0]["code"] == "UNKNOWN_SYMBOL"


@pytest.mark.parametrize("payload", [
    {"symbol": "AA$PL", "question": "How is it doing?"},
    {"symbol": "AAPL", "question": "?"},
    {"symbol": "AAPL", "question": "How is it doing?", "period": "2w"},
])
def test_invalid_request_is_422(client, payload):
    assert client.post("/analyze", json=payload).status_code == 422


def test_report_endpoint_returns_markdown(client):
    r = client.post("/analyze/report", json={"symbol": "MSFT", "question": "Latest news?"})
    assert r.status_code == 200
    assert "DATA TIMESTAMP MISMATCH" in r.text
    assert "## Reported facts (from source data)" in r.text


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"
