from __future__ import annotations

from functools import lru_cache

from core.config import Settings, get_settings
from tools.providers.base import (
    MarketDataProvider,
    NoDataAvailable,
    ProviderError,
    ProviderUnavailable,
    SymbolNotFound,
)
from tools.providers.mock_provider import MockMarketDataProvider


def build_provider(settings: Settings) -> MarketDataProvider:
    """Market data for stocks/indices, with MF<code> symbols routed to AMFI mutual-fund data."""
    from tools.providers.amfi_provider import AmfiProvider, CompositeProvider

    provider = CompositeProvider(_market_provider(settings), AmfiProvider(cache_dir=settings.db_path.parent / "amfi"))
    if settings.data_provider == "yahoo":
        # Indian stocks get the official BSE filings alongside the provider's news.
        from core.universes import shared_universes
        from tools.providers.bse import BseFilings, BseFilingsProvider

        registry = shared_universes(settings.db_path.parent / "listings")
        return BseFilingsProvider(provider, BseFilings(timeout_s=settings.tool_timeout_seconds),
                                  lambda symbol: registry.venues(symbol)["bse_code"])
    return provider


def _market_provider(settings: Settings) -> MarketDataProvider:
    if settings.data_provider == "mock":
        return MockMarketDataProvider(settings.mock_data_path)
    if settings.data_provider == "yahoo":
        from tools.providers.yahoo_provider import YahooFinanceProvider

        return YahooFinanceProvider(timeout_s=settings.tool_timeout_seconds)
    if settings.data_provider == "alpha_vantage":
        from tools.providers.alpha_vantage import AlphaVantageProvider

        if settings.alpha_vantage_api_key is None:
            raise RuntimeError("DATA_PROVIDER=alpha_vantage requires ALPHA_VANTAGE_API_KEY")
        return AlphaVantageProvider(settings.alpha_vantage_api_key)
    raise ValueError(f"unknown data provider {settings.data_provider!r}")


@lru_cache(maxsize=1)
def get_default_provider() -> MarketDataProvider:
    return build_provider(get_settings())


__all__ = [
    "MarketDataProvider",
    "MockMarketDataProvider",
    "NoDataAvailable",
    "ProviderError",
    "ProviderUnavailable",
    "SymbolNotFound",
    "build_provider",
    "get_default_provider",
]
