"""The state handed from the data-fetching stage to everything downstream."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tools.errors import InvalidInputError, ToolError, UnknownSymbolError
from tools.models import Announcements, PriceHistory, PriceQuote, SourceRef, ToolResponse

# tool name -> (bundle attribute, human label)
DATASETS: dict[str, tuple[str, str]] = {
    "get_stock_price": ("quote", "price quote feed"),
    "get_price_history": ("history", "price history feed"),
    "get_announcements": ("announcements", "announcements feed"),
}

FATAL_ERRORS: tuple[type[ToolError], ...] = (UnknownSymbolError, InvalidInputError)


@dataclass(frozen=True)
class PlannedCall:
    tool: str
    arguments: dict[str, Any]


@dataclass
class DataBundle:
    symbol: str
    plan: list[PlannedCall]
    mode: str
    window_days: int = 30  # calendar days used for the "recent window" metrics
    quote: ToolResponse[PriceQuote] | None = None
    history: ToolResponse[PriceHistory] | None = None
    announcements: ToolResponse[Announcements] | None = None
    errors: dict[str, ToolError] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def store(self, tool: str, response: ToolResponse[Any]) -> None:
        setattr(self, DATASETS[tool][0], response)
        self.errors.pop(tool, None)

    def get(self, tool: str) -> ToolResponse[Any] | None:
        return getattr(self, DATASETS[tool][0])

    def available(self) -> list[str]:
        return [tool for tool in DATASETS if self.get(tool) is not None]

    def missing(self) -> list[str]:
        return [tool for tool in DATASETS if self.get(tool) is None]

    def fatal_error(self) -> ToolError | None:
        return next((e for e in self.errors.values() if isinstance(e, FATAL_ERRORS)), None)

    def sources(self) -> list[SourceRef]:
        refs = [r.source for r in (self.quote, self.history, self.announcements) if r is not None]
        if self.announcements is not None:
            refs += [
                SourceRef(provider=self.announcements.source.provider, source_id=item.url)
                for item in self.announcements.data.items
                if item.url
            ]
        seen: dict[str, SourceRef] = {}
        for ref in refs:
            seen.setdefault(ref.source_id, ref)
        return list(seen.values())
