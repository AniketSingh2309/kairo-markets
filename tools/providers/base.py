"""Provider interface: the only seam that changes when swapping mock -> real API.

A provider returns a *raw* payload (as an HTTP API would) in this envelope::

    {
        "meta": {"source_id": str, "url": str | None, "observed_at": ISO-8601 str},
        "data": {...endpoint specific...},
    }

Providers do NOT validate payloads -- that is the tool layer's job, so a
buggy or changed upstream API surfaces as ``MalformedResponseError`` instead of
leaking bad data into the pipeline. Providers signal known failure modes by
raising the provider-level exceptions below; the tool layer maps them onto the
typed ``ToolError`` hierarchy.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Protocol, runtime_checkable


class ProviderError(Exception):
    """Base class for provider-level failures."""


class SymbolNotFound(ProviderError):
    """The provider has no such symbol."""


class NoDataAvailable(ProviderError):
    """The symbol exists but there is no data for the requested date/window."""


class ProviderUnavailable(ProviderError):
    """Transport/outage failure; may succeed on retry."""


@runtime_checkable
class MarketDataProvider(Protocol):
    name: str

    def fetch_quote(self, symbol: str, on_date: dt.date | None) -> dict[str, Any]:
        """Daily OHLCV bar for ``on_date`` (or the latest bar when None).

        data = {"symbol", "currency", "bar": {"date","open","high","low","close","volume"}}
        """
        ...

    def fetch_price_history(self, symbol: str, lookback_days: int) -> dict[str, Any]:
        """Daily bars covering ``lookback_days`` back from the latest available bar.

        data = {"symbol", "currency", "bars": [bar, ...]}
        """
        ...

    def fetch_profile(self, symbol: str) -> dict[str, Any]:
        """Company profile. data = {"symbol","name","sector","industry","quote_type","exchange"}"""
        ...

    def fetch_live(self, symbol: str) -> dict[str, Any]:
        """Intraday snapshot for the live chart (not used by the analysis pipeline).

        data = {"symbol","currency","price","market_time","previous_close","day_high","day_low",
                "market_state": "open"|"closed","session_start","session_end",
                "points": [{"t": ISO-8601, "price"}, ...]}
        """
        ...

    def fetch_announcements(self, symbol: str, lookback_days: int) -> dict[str, Any]:
        """Company announcements within ``lookback_days`` of the feed's as-of time.

        data = {"symbol", "items": [{"id","published_at","category","title","summary","url"}]}
        """
        ...
