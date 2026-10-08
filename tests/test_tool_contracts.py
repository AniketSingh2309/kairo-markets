"""Tool contract: valid input, invalid input, unknown symbol, timeout, malformed response."""

from __future__ import annotations

import datetime as dt
import time

import pytest

from tests.conftest import (
    REFERENCE_TIME,
    CountingProvider,
    FixedPayloadProvider,
    FlakyProvider,
    SlowProvider,
    valid_quote_payload,
)
from tools import (
    DataNotFoundError,
    DataSourceUnavailableError,
    InvalidInputError,
    MalformedResponseError,
    ToolError,
    ToolTimeoutError,
    UnknownSymbolError,
    get_announcements,
    get_price_history,
    get_stock_price,
)
from tools.providers import ProviderUnavailable

UTC = dt.timezone.utc


def call_kwargs(provider, clock, timeout_s=0.5):
    return {"provider": provider, "clock": clock, "timeout_s": timeout_s}


# --- valid input ----------------------------------------------------------------


def test_get_stock_price_returns_data_source_and_timestamps(mock_provider, clock):
    resp = get_stock_price("AAPL", **call_kwargs(mock_provider, clock))
    assert resp.tool == "get_stock_price"
    assert resp.data.symbol == "AAPL"
    assert resp.data.bar.close == pytest.approx(246.10)
    assert resp.data.bar.date == dt.date(2026, 9, 29)
    assert resp.source.provider == "mock"
    assert resp.source.source_id.startswith("mock://market-data/v1/quote/AAPL")
    assert resp.observed_at == dt.datetime(2026, 9, 29, 20, 0, tzinfo=UTC)
    assert resp.observed_at.tzinfo is not None
    assert resp.fetched_at == REFERENCE_TIME
    assert resp.query == {"symbol": "AAPL", "date": None}


def test_symbol_is_normalised(mock_provider, clock):
    resp = get_stock_price("  aapl ", **call_kwargs(mock_provider, clock))
    assert resp.data.symbol == "AAPL"


def test_get_stock_price_for_specific_date(mock_provider, clock):
    resp = get_stock_price("AAPL", "2026-09-15", **call_kwargs(mock_provider, clock))
    assert resp.data.bar.date == dt.date(2026, 9, 15)
    assert resp.data.bar.close == pytest.approx(238.40)
    assert resp.observed_at == dt.datetime(2026, 9, 15, 20, 0, tzinfo=UTC)


def test_get_price_history_valid(mock_provider, clock):
    resp = get_price_history("AAPL", "30d", **call_kwargs(mock_provider, clock))
    dates = [b.date for b in resp.data.bars]
    assert len(dates) == 21
    assert dates == sorted(dates)
    assert resp.observed_at == dt.datetime(2026, 9, 29, 20, 0, tzinfo=UTC)


def test_get_announcements_valid(mock_provider, clock):
    resp = get_announcements("AAPL", "30d", **call_kwargs(mock_provider, clock))
    assert [a.id for a in resp.data.items] == ["AAPL-2026-091", "AAPL-2026-094", "AAPL-2026-097"]
    assert resp.observed_at == dt.datetime(2026, 9, 29, 22, 0, tzinfo=UTC)
    assert resp.query == {"symbol": "AAPL", "period": "30d"}


def test_announcements_period_filters_items(mock_provider, clock):
    resp = get_announcements("AAPL", "7d", **call_kwargs(mock_provider, clock))
    assert [a.id for a in resp.data.items] == ["AAPL-2026-097"]


# --- invalid input: rejected before the provider is called ------------------------


@pytest.mark.parametrize("symbol", ["", "   ", "123", "AAPL$", "TOOLONGSYMBOL1", "AA PL", None, 42])
def test_invalid_symbol_rejected_before_provider_call(mock_provider, clock, symbol):
    provider = CountingProvider(mock_provider)
    with pytest.raises(InvalidInputError) as info:
        get_stock_price(symbol, **call_kwargs(provider, clock))
    assert info.value.code == "INVALID_INPUT"
    assert provider.total == 0


@pytest.mark.parametrize("period", ["2w", "30", "", "10y", None])
def test_invalid_period_rejected_before_provider_call(mock_provider, clock, period):
    provider = CountingProvider(mock_provider)
    with pytest.raises(InvalidInputError):
        get_announcements("AAPL", period, **call_kwargs(provider, clock))
    with pytest.raises(InvalidInputError):
        get_price_history("AAPL", period, **call_kwargs(provider, clock))
    assert provider.total == 0


@pytest.mark.parametrize("date", ["2026-13-01", "yesterday", "1969-12-31"])
def test_invalid_date_rejected(mock_provider, clock, date):
    provider = CountingProvider(mock_provider)
    with pytest.raises(InvalidInputError):
        get_stock_price("AAPL", date, **call_kwargs(provider, clock))
    assert provider.total == 0


def test_future_date_rejected(mock_provider, clock):
    provider = CountingProvider(mock_provider)
    with pytest.raises(InvalidInputError, match="in the future"):
        get_stock_price("AAPL", "2026-10-01", **call_kwargs(provider, clock))
    assert provider.total == 0


# --- unknown symbol / no data ---------------------------------------------------------


def test_unknown_symbol_raises_named_error(mock_provider, clock):
    with pytest.raises(UnknownSymbolError) as info:
        get_stock_price("ZZZZ", **call_kwargs(mock_provider, clock))
    err = info.value
    assert err.code == "UNKNOWN_SYMBOL"
    assert err.symbol == "ZZZZ"
    assert err.tool == "get_stock_price"
    assert err.retryable is False


def test_unknown_symbol_on_every_tool(mock_provider, clock):
    for tool in (get_stock_price, get_price_history, get_announcements):
        with pytest.raises(UnknownSymbolError):
            tool("ZZZZ", **call_kwargs(mock_provider, clock))


def test_known_symbol_without_bar_on_date_is_data_not_found(mock_provider, clock):
    # 2026-09-26 is a Saturday.
    with pytest.raises(DataNotFoundError) as info:
        get_stock_price("AAPL", "2026-09-26", **call_kwargs(mock_provider, clock))
    assert not isinstance(info.value, UnknownSymbolError)


# --- timeout ---------------------------------------------------------------------


def test_timeout_raises_typed_error_quickly(mock_provider, clock):
    provider = SlowProvider(mock_provider, delay=0.5)
    start = time.perf_counter()
    with pytest.raises(ToolTimeoutError) as info:
        get_stock_price("AAPL", **call_kwargs(provider, clock, timeout_s=0.05))
    elapsed = time.perf_counter() - start
    assert elapsed < 0.4, "tool must stop waiting at the timeout, not when the provider returns"
    assert info.value.code == "TOOL_TIMEOUT"
    assert info.value.retryable is True


def test_provider_outage_maps_to_source_unavailable(mock_provider, clock):
    provider = FlakyProvider(mock_provider, failures=1, exc=ProviderUnavailable("503"))
    with pytest.raises(DataSourceUnavailableError) as info:
        get_stock_price("AAPL", **call_kwargs(provider, clock))
    assert info.value.retryable is True


# --- malformed response ----------------------------------------------------------


def test_malformed_rows_in_mock_feed(mock_provider, clock):
    with pytest.raises(MalformedResponseError) as info:
        get_stock_price("BADF", **call_kwargs(mock_provider, clock))
    assert info.value.code == "MALFORMED_RESPONSE"
    with pytest.raises(MalformedResponseError):
        get_price_history("BADF", **call_kwargs(mock_provider, clock))


def _mutate(path: list, value):
    payload = valid_quote_payload()
    target = payload
    for key in path[:-1]:
        target = target[key]
    if value is _DELETE:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return payload


_DELETE = object()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "not a mapping",
        {"data": valid_quote_payload()["data"]},  # no meta
        _mutate(["meta", "source_id"], _DELETE),
        _mutate(["meta", "observed_at"], _DELETE),
        _mutate(["meta", "observed_at"], "2026-09-29T20:00:00"),  # naive timestamp
        _mutate(["meta", "observed_at"], "2027-01-01T00:00:00Z"),  # in the future
        _mutate(["data", "bar", "close"], "N/A"),
        _mutate(["data", "bar", "close"], -1),
        _mutate(["data", "bar", "high"], 1.0),  # high < low
        _mutate(["data", "bar", "volume"], None),
        _mutate(["data", "currency"], "DOLLARS"),
        _mutate(["data", "symbol"], "MSFT"),  # payload for another symbol
    ],
    ids=["none", "string", "no-meta", "no-source-id", "no-observed-at", "naive-ts", "future-ts",
         "close-NA", "negative-close", "high-below-low", "null-volume", "bad-currency", "wrong-symbol"],
)
def test_malformed_payloads_raise_malformed_response(clock, payload):
    provider = FixedPayloadProvider(payload)
    with pytest.raises(MalformedResponseError):
        get_stock_price("AAPL", **call_kwargs(provider, clock))


def test_bar_for_wrong_date_is_malformed(clock):
    provider = FixedPayloadProvider(valid_quote_payload())  # bar is 2026-09-29
    with pytest.raises(MalformedResponseError, match="asked for 2026-09-15"):
        get_stock_price("AAPL", "2026-09-15", **call_kwargs(provider, clock))


def test_valid_fixed_payload_passes(clock):
    resp = get_stock_price("AAPL", **call_kwargs(FixedPayloadProvider(valid_quote_payload()), clock))
    assert resp.source.source_id == "mock://test/quote/AAPL"


def test_error_cases_are_distinct_types():
    distinct = [UnknownSymbolError, MalformedResponseError, ToolTimeoutError, InvalidInputError,
                DataSourceUnavailableError, DataNotFoundError]
    for a in distinct:
        assert issubclass(a, ToolError)
        for b in distinct:
            if a is not b:
                assert not issubclass(a, b)
    assert len({cls.code for cls in distinct}) == len(distinct)
