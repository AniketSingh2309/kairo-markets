from __future__ import annotations

import copy
import datetime as dt
import time
from collections import Counter
from typing import Any

import pytest

from core.clock import fixed_clock
from core.config import Settings
from tools.providers import MockMarketDataProvider

REFERENCE_TIME = dt.datetime(2026, 9, 30, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def settings() -> Settings:
    # Built directly (not from env) so a developer's .env never affects tests.
    return Settings(
        llm_provider="stub",
        tool_timeout_seconds=0.5,
        tool_max_retries=2,
        tool_backoff_base_seconds=0.0,
        max_tool_calls_per_request=8,
    )


@pytest.fixture
def clock():
    return fixed_clock(REFERENCE_TIME)


@pytest.fixture
def mock_provider() -> MockMarketDataProvider:
    # No real sleeping: SLOW's simulated latency is skipped here; timeout tests use SlowProvider.
    return MockMarketDataProvider(sleep=lambda _s: None)


class CountingProvider:
    """Wraps a provider and counts calls per endpoint."""

    def __init__(self, inner: Any):
        self.inner = inner
        self.name = inner.name
        self.calls: Counter[str] = Counter()

    def fetch_quote(self, symbol, on_date):
        self.calls["quote"] += 1
        return self.inner.fetch_quote(symbol, on_date)

    def fetch_price_history(self, symbol, lookback_days):
        self.calls["history"] += 1
        return self.inner.fetch_price_history(symbol, lookback_days)

    def fetch_announcements(self, symbol, lookback_days):
        self.calls["announcements"] += 1
        return self.inner.fetch_announcements(symbol, lookback_days)

    @property
    def total(self) -> int:
        return sum(self.calls.values())


class SlowProvider(CountingProvider):
    """Sleeps before delegating -- used to trigger the tool timeout."""

    def __init__(self, inner: Any, delay: float):
        super().__init__(inner)
        self.delay = delay

    def fetch_quote(self, symbol, on_date):
        time.sleep(self.delay)
        return super().fetch_quote(symbol, on_date)

    def fetch_price_history(self, symbol, lookback_days):
        time.sleep(self.delay)
        return super().fetch_price_history(symbol, lookback_days)

    def fetch_announcements(self, symbol, lookback_days):
        time.sleep(self.delay)
        return super().fetch_announcements(symbol, lookback_days)


class FlakyProvider(CountingProvider):
    """Raises ``exc`` for the first ``failures`` calls of each endpoint, then delegates."""

    def __init__(self, inner: Any, failures: int, exc: Exception):
        super().__init__(inner)
        self.failures = failures
        self.exc = exc

    def _maybe_fail(self, endpoint: str) -> None:
        if self.calls[endpoint] <= self.failures:
            raise self.exc

    def fetch_quote(self, symbol, on_date):
        self.calls["quote"] += 1
        self._maybe_fail("quote")
        return self.inner.fetch_quote(symbol, on_date)


class FixedPayloadProvider(CountingProvider):
    """Returns a fixed (possibly malformed) payload from fetch_quote."""

    def __init__(self, payload: Any):
        super().__init__(MockMarketDataProvider(sleep=lambda _s: None))
        self.payload = payload

    def fetch_quote(self, symbol, on_date):
        self.calls["quote"] += 1
        return copy.deepcopy(self.payload)


def valid_quote_payload(symbol: str = "AAPL") -> dict[str, Any]:
    return {
        "meta": {"source_id": f"mock://test/quote/{symbol}", "url": None,
                 "observed_at": "2026-09-29T20:00:00Z"},
        "data": {"symbol": symbol, "currency": "USD",
                 "bar": {"date": "2026-09-29", "open": 245.4, "high": 246.8, "low": 244.9,
                         "close": 246.1, "volume": 50870000}},
    }
