"""Retries with backoff, retry bounds, and the per-request tool-call budget."""

from __future__ import annotations

import pytest

from tests.conftest import CountingProvider, FlakyProvider, SlowProvider
from tools.errors import (
    DataSourceUnavailableError,
    InvalidInputError,
    ToolBudgetExceededError,
    ToolTimeoutError,
    UnknownSymbolError,
)
from tools.executor import ToolExecutor
from tools.providers import ProviderUnavailable


def make_executor(provider, settings, clock, **overrides):
    sleeps: list[float] = []
    executor = ToolExecutor(provider=provider, settings=settings.with_overrides(**overrides),
                            clock=clock, sleep=sleeps.append)
    return executor, sleeps


def test_transient_failure_is_retried_with_exponential_backoff(mock_provider, settings, clock):
    provider = FlakyProvider(mock_provider, failures=2, exc=ProviderUnavailable("503"))
    executor, sleeps = make_executor(provider, settings, clock, tool_backoff_base_seconds=0.25)

    resp = executor.call("get_stock_price", symbol="AAPL")

    assert resp.data.bar.close == pytest.approx(246.10)
    assert provider.calls["quote"] == 3
    assert sleeps == [0.25, 0.5]
    assert [r.status for r in executor.ledger] == ["error", "error", "ok"]
    assert [r.attempt for r in executor.ledger] == [1, 2, 3]
    assert executor.ledger[-1].observed_at == resp.observed_at
    assert executor.budget.used == 3


def test_retries_are_bounded(mock_provider, settings, clock):
    provider = FlakyProvider(mock_provider, failures=99, exc=ProviderUnavailable("down"))
    executor, _ = make_executor(provider, settings, clock, tool_max_retries=2)
    with pytest.raises(DataSourceUnavailableError):
        executor.call("get_stock_price", symbol="AAPL")
    assert provider.calls["quote"] == 3  # 1 attempt + 2 retries


def test_timeouts_are_retried_then_surface_as_timeout(mock_provider, settings, clock):
    provider = SlowProvider(mock_provider, delay=0.3)
    executor, _ = make_executor(provider, settings, clock, tool_timeout_seconds=0.02, tool_max_retries=1)
    with pytest.raises(ToolTimeoutError):
        executor.call("get_stock_price", symbol="AAPL")
    assert [r.error_code for r in executor.ledger] == ["TOOL_TIMEOUT", "TOOL_TIMEOUT"]


def test_non_retryable_errors_are_not_retried(mock_provider, settings, clock):
    provider = CountingProvider(mock_provider)
    executor, sleeps = make_executor(provider, settings, clock)
    with pytest.raises(UnknownSymbolError):
        executor.call("get_stock_price", symbol="ZZZZ")
    assert provider.total == 1
    assert sleeps == []


def test_invalid_input_consumes_no_budget(mock_provider, settings, clock):
    provider = CountingProvider(mock_provider)
    executor, _ = make_executor(provider, settings, clock)
    with pytest.raises(InvalidInputError):
        executor.call("get_announcements", symbol="AAPL", period="forever")
    assert provider.total == 0
    assert executor.budget.used == 0
    assert executor.ledger[0].status == "rejected"


def test_unknown_tool_is_rejected(mock_provider, settings, clock):
    executor, _ = make_executor(mock_provider, settings, clock)
    with pytest.raises(InvalidInputError):
        executor.call("delete_everything", symbol="AAPL")


def test_budget_caps_total_calls(mock_provider, settings, clock):
    provider = CountingProvider(mock_provider)
    executor, _ = make_executor(provider, settings, clock, max_tool_calls_per_request=2)
    executor.call("get_stock_price", symbol="AAPL")
    executor.call("get_price_history", symbol="AAPL")
    with pytest.raises(ToolBudgetExceededError):
        executor.call("get_announcements", symbol="AAPL")
    assert provider.total == 2
    assert executor.ledger[-1].status == "rejected"
    assert executor.ledger[-1].error_code == "TOOL_BUDGET_EXCEEDED"


def test_retries_count_against_budget(mock_provider, settings, clock):
    provider = FlakyProvider(mock_provider, failures=99, exc=ProviderUnavailable("down"))
    executor, _ = make_executor(provider, settings, clock, max_tool_calls_per_request=2,
                                tool_max_retries=3)
    with pytest.raises(ToolBudgetExceededError):
        executor.call("get_stock_price", symbol="AAPL")
    assert provider.calls["quote"] == 2
