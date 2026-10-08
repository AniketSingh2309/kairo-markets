"""Tool contract layer: typed, validated, time-bounded access to market data."""

from tools.errors import (
    DataNotFoundError,
    DataSourceUnavailableError,
    InvalidInputError,
    MalformedResponseError,
    ToolBudgetExceededError,
    ToolError,
    ToolTimeoutError,
    UnknownSymbolError,
)
from tools.market_tools import TOOL_SPECS, get_announcements, get_price_history, get_stock_price
from tools.models import SourceRef, ToolResponse

__all__ = [
    "TOOL_SPECS",
    "DataNotFoundError",
    "DataSourceUnavailableError",
    "InvalidInputError",
    "MalformedResponseError",
    "SourceRef",
    "ToolBudgetExceededError",
    "ToolError",
    "ToolResponse",
    "ToolTimeoutError",
    "UnknownSymbolError",
    "get_announcements",
    "get_price_history",
    "get_stock_price",
]
