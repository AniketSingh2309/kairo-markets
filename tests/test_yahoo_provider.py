"""Yahoo Finance provider, via the tool contract, against a fake HTTP transport (no network)."""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from tools import (
    DataNotFoundError,
    DataSourceUnavailableError,
    MalformedResponseError,
    UnknownSymbolError,
    get_announcements,
    get_price_history,
    get_stock_price,
)
from tools.providers.yahoo_provider import BASE_URL, YahooFinanceProvider

UTC = dt.timezone.utc
NOW = dt.datetime.now(UTC)
# Two completed sessions (09:30 New York open timestamps) plus today's live bar.
DAY1 = dt.datetime(2026, 9, 28, 13, 30, tzinfo=UTC)
DAY2 = dt.datetime(2026, 9, 29, 13, 30, tzinfo=UTC)
LAST_TRADE = dt.datetime(2026, 9, 29, 20, 0, tzinfo=UTC)


def chart(symbol="AAPL", closes=(338.3999938964844, 329.3999938964844), **quote_overrides):
    quote = {
        "open": [340.37, 336.97], "high": [342.99, 337.09], "low": [338.04, 328.70],
        "close": list(closes), "volume": [32820800, 38427200],
    }
    quote.update(quote_overrides)
    return {"chart": {"error": None, "result": [{
        "meta": {"symbol": symbol, "currency": "USD", "gmtoffset": -14400,
                 "regularMarketTime": int(LAST_TRADE.timestamp()),
                 "currentTradingPeriod": {"regular": {"start": 1790775000, "end": 1790798400,
                                                      "gmtoffset": -14400}}},
        "timestamp": [int(DAY1.timestamp()), int(DAY2.timestamp())],
        "indicators": {"quote": [quote]},
    }]}}


def search(symbol="AAPL"):
    recent = int((NOW - dt.timedelta(hours=3)).timestamp())
    old = int((NOW - dt.timedelta(days=60)).timestamp())
    return {"quotes": [{"symbol": symbol}], "news": [
        {"uuid": "n1", "title": "Apple ships a thing", "publisher": "Wire", "link": "https://x/n1",
         "providerPublishTime": recent, "relatedTickers": [symbol]},
        {"uuid": "n2", "title": "Old news", "publisher": "Wire", "link": "https://x/n2",
         "providerPublishTime": old, "relatedTickers": [symbol]},
        {"uuid": "n3", "title": "About another company", "publisher": "Wire", "link": "https://x/n3",
         "providerPublishTime": recent, "relatedTickers": ["MSFT"]},
    ]}


def provider(handler):
    client = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    return YahooFinanceProvider(client=client)


def respond(payload, status=200):
    return lambda request: httpx.Response(status, content=json.dumps(payload))


def test_latest_quote_parsed_and_timestamped():
    resp = get_stock_price("AAPL", provider=provider(respond(chart())), timeout_s=2)
    bar = resp.data.bar
    assert bar.date == dt.date(2026, 9, 29)
    assert bar.close == 329.4  # float32 artefact rounded away
    assert resp.observed_at == LAST_TRADE  # regularMarketTime for the latest session
    assert resp.source.provider == "yahoo_finance"
    assert resp.source.source_id.startswith(BASE_URL)


def test_older_bar_observed_at_is_session_close():
    resp = get_stock_price("AAPL", "2026-09-28", provider=provider(respond(chart())), timeout_s=2)
    assert resp.observed_at == dt.datetime(2026, 9, 28, 20, 0, tzinfo=UTC)  # 16:00 New York


def test_history_drops_empty_placeholder_bars():
    payload = chart()
    result = payload["chart"]["result"][0]
    result["timestamp"].append(int(dt.datetime(2026, 9, 30, 13, 30, tzinfo=UTC).timestamp()))
    for key in ("open", "high", "low", "close", "volume"):
        result["indicators"]["quote"][0][key].append(None)
    resp = get_price_history("AAPL", "30d", provider=provider(respond(payload)), timeout_s=2)
    assert [b.date.isoformat() for b in resp.data.bars] == ["2026-09-28", "2026-09-29"]


def test_partially_null_bar_is_malformed():
    bad = chart(close=[338.4, None])
    with pytest.raises(MalformedResponseError):
        get_price_history("AAPL", "30d", provider=provider(respond(bad)), timeout_s=2)


def test_unknown_symbol_404():
    err = {"chart": {"result": None, "error": {"code": "Not Found",
                                               "description": "No data found, symbol may be delisted"}}}
    with pytest.raises(UnknownSymbolError):
        get_stock_price("ZZZZ", provider=provider(respond(err, 404)), timeout_s=2)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_rate_limit_and_server_errors_are_unavailable(status):
    with pytest.raises(DataSourceUnavailableError):
        get_stock_price("AAPL", provider=provider(respond({}, status)), timeout_s=2)


def test_network_error_is_unavailable():
    def boom(request):
        raise httpx.ConnectError("no route")
    with pytest.raises(DataSourceUnavailableError):
        get_stock_price("AAPL", provider=provider(boom), timeout_s=2)


def test_changed_response_shape_is_malformed():
    with pytest.raises(MalformedResponseError):
        get_stock_price("AAPL", provider=provider(respond({"something": "else"})), timeout_s=2)


def test_missing_date_is_data_not_found():
    with pytest.raises(DataNotFoundError):
        get_stock_price("AAPL", "2026-09-27", provider=provider(respond(chart())), timeout_s=2)


def test_news_filtered_by_ticker_and_period():
    resp = get_announcements("AAPL", "7d", provider=provider(respond(search())), timeout_s=2)
    assert [a.id for a in resp.data.items] == ["n1"]
    item = resp.data.items[0]
    assert item.url == "https://x/n1" and item.category == "news: Wire"
    assert abs((resp.observed_at - NOW).total_seconds()) < 60  # live feed observed at fetch time
