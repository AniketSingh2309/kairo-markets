"""Placeholder for a real Alpha Vantage provider.

Not implemented yet -- it exists to show exactly what a real provider has to
do to plug into the tool contract. Implement the three methods so that they:

1. Call the endpoint with an HTTP client that has its own timeout
   (the tool layer adds an outer timeout too):
     * fetch_quote          -> TIME_SERIES_DAILY (pick the bar for on_date / latest)
     * fetch_price_history  -> TIME_SERIES_DAILY (outputsize=compact), trim to lookback
     * fetch_announcements  -> NEWS_SENTIMENT (tickers=symbol, time_from=...)
2. Translate provider responses into the envelope documented in
   ``tools.providers.base`` -- ``meta.observed_at`` must come from the payload
   (e.g. "Meta Data" -> "3. Last Refreshed" plus the exchange close time),
   never from the local clock.
3. Raise ``SymbolNotFound`` for Alpha Vantage's "Invalid API call" on an
   unknown symbol, ``ProviderUnavailable`` for HTTP 5xx / connection errors /
   rate-limit notes, and ``NoDataAvailable`` for an empty window.
4. Pass anything else through untouched; the tool layer's Pydantic models are
   what detect malformed payloads.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import SecretStr


class AlphaVantageProvider:
    name = "alpha_vantage"
    base_url = "https://www.alphavantage.co/query"

    def __init__(self, api_key: SecretStr):
        self._api_key = api_key

    def fetch_quote(self, symbol: str, on_date: dt.date | None) -> dict[str, Any]:
        raise NotImplementedError("AlphaVantageProvider.fetch_quote is not implemented yet")

    def fetch_price_history(self, symbol: str, lookback_days: int) -> dict[str, Any]:
        raise NotImplementedError("AlphaVantageProvider.fetch_price_history is not implemented yet")

    def fetch_announcements(self, symbol: str, lookback_days: int) -> dict[str, Any]:
        raise NotImplementedError("AlphaVantageProvider.fetch_announcements is not implemented yet")
