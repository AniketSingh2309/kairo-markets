"""Typed errors for the tool contract layer.

Every failure mode of an external-data tool maps to exactly one of these
classes. Tools never return a silent ``None``: they either return a validated
``ToolResponse`` or raise one of the errors below.

``retryable`` tells the executor whether a bounded retry makes sense:
timeouts and transient unavailability are worth retrying; an unknown symbol,
bad input or a structurally malformed payload will not fix itself on retry.
"""

from __future__ import annotations

from typing import Any


class ToolError(Exception):
    code: str = "TOOL_ERROR"
    retryable: bool = False

    def __init__(self, message: str, *, tool: str | None = None, symbol: str | None = None):
        super().__init__(message)
        self.message = message
        self.tool = tool
        self.symbol = symbol

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "tool": self.tool,
            "symbol": self.symbol,
            "retryable": self.retryable,
        }


class InvalidInputError(ToolError):
    """Arguments failed validation; the data source was never called."""

    code = "INVALID_INPUT"


class UnknownSymbolError(ToolError):
    """The symbol is well-formed but the data source does not know it."""

    code = "UNKNOWN_SYMBOL"


class DataNotFoundError(ToolError):
    """The symbol exists but has no data for the requested date/period."""

    code = "DATA_NOT_FOUND"


class MalformedResponseError(ToolError):
    """The data source answered, but the payload failed schema/consistency checks."""

    code = "MALFORMED_RESPONSE"


class ToolTimeoutError(ToolError):
    """The data source did not answer within the per-call timeout."""

    code = "TOOL_TIMEOUT"
    retryable = True


class DataSourceUnavailableError(ToolError):
    """Transport-level failure (connection refused, 5xx, provider outage)."""

    code = "SOURCE_UNAVAILABLE"
    retryable = True


class ToolBudgetExceededError(ToolError):
    """The per-request tool-call budget is exhausted; no further calls are made."""

    code = "TOOL_BUDGET_EXCEEDED"
