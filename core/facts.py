"""Turn tool responses into a registry of citable facts.

Everything numeric the final answer may say must exist here first. Reported
facts are copied from tool payloads; computed facts (moving averages, %
change, volatility) are derived deterministically in Python -- the LLM never
does arithmetic.

Fact id prefixes: P = reported price field, N = reported announcement,
C = computed metric.
"""

from __future__ import annotations

import datetime as dt
import statistics
from typing import Any

from core.bundle import DataBundle
from core.config import Settings
from core.schemas import Fact, Flag
from tools.models import PriceBar


# Only the most recent announcements become facts (keeps LLM prompts within rate limits).
MAX_ANNOUNCEMENT_FACTS = 8


class FactRegistry:
    def __init__(self) -> None:
        self._facts: dict[str, Fact] = {}
        self._counters: dict[str, int] = {}

    def add(self, prefix: str, **fields: Any) -> Fact:
        n = self._counters.get(prefix, 0) + 1
        self._counters[prefix] = n
        fact = Fact(id=f"{prefix}{n}", **fields)
        self._facts[fact.id] = fact
        return fact

    def get(self, fact_id: str) -> Fact | None:
        return self._facts.get(fact_id)

    def __contains__(self, fact_id: object) -> bool:
        return fact_id in self._facts

    def __len__(self) -> int:
        return len(self._facts)

    def all(self) -> list[Fact]:
        return list(self._facts.values())

    def by_kind(self, kind: str) -> list[Fact]:
        return [f for f in self._facts.values() if f.kind == kind]


def _money(value: float, currency: str) -> str:
    return f"{value:,.2f} {currency}"


def _add_bar_facts(
    reg: FactRegistry, bar: PriceBar, currency: str, source_id: str, observed_at: Any, feed: str
) -> None:
    day = bar.date.isoformat()
    common = {"kind": "reported", "source_id": source_id, "observed_at": observed_at}
    reg.add("P", label=f"Close price on {day} ({feed})", value=bar.close, unit=currency,
            display=_money(bar.close, currency), **common)
    reg.add("P", label=f"Open price on {day}", value=bar.open, unit=currency,
            display=_money(bar.open, currency), **common)
    reg.add("P", label=f"Intraday high on {day}", value=bar.high, unit=currency,
            display=_money(bar.high, currency), **common)
    reg.add("P", label=f"Intraday low on {day}", value=bar.low, unit=currency,
            display=_money(bar.low, currency), **common)
    reg.add("P", label=f"Volume on {day}", value=bar.volume, unit="shares",
            display=f"{bar.volume:,} shares", **common)


def _add_history_metrics(reg: FactRegistry, bundle: DataBundle, flags: list[Flag]) -> None:
    history = bundle.history
    assert history is not None
    last_day = history.data.bars[-1].date
    cutoff = last_day - dt.timedelta(days=bundle.window_days)
    bars = [b for b in history.data.bars if b.date >= cutoff]
    cur = history.data.currency
    closes = [b.close for b in bars]
    first, last = bars[0], bars[-1]
    common = {"kind": "computed", "source_id": history.source.source_id,
              "observed_at": history.observed_at}

    reg.add("C", label="Trading days in price window", value=len(bars), unit="days",
            display=f"{len(bars)} trading days ({first.date.isoformat()} to {last.date.isoformat()})",
            method="count of daily bars returned by get_price_history", **common)

    if len(bars) >= 2:
        change = (last.close - first.close) / first.close * 100
        reg.add("C", label=f"Close-to-close change {first.date.isoformat()} to {last.date.isoformat()}",
                value=round(change, 2), unit="%", display=f"{change:+.2f}%",
                method=f"(close {last.close} - close {first.close}) / close {first.close}", **common)

    if len(bars) >= 5:
        sma5 = statistics.fmean(closes[-5:])
        reg.add("C", label=f"5-day simple moving average of close (as of {last.date.isoformat()})",
                value=round(sma5, 2), unit=cur, display=_money(sma5, cur),
                method="mean of the last 5 closes", **common)

    if len(bars) >= 20:
        sma20 = statistics.fmean(closes[-20:])
        reg.add("C", label=f"20-day simple moving average of close (as of {last.date.isoformat()})",
                value=round(sma20, 2), unit=cur, display=_money(sma20, cur),
                method="mean of the last 20 closes", **common)
        gap = (last.close - sma20) / sma20 * 100
        reg.add("C", label="Latest close relative to 20-day SMA", value=round(gap, 2), unit="%",
                display=f"{gap:+.2f}%", method="(last close - SMA20) / SMA20", **common)
    else:
        flags.append(Flag(
            code="INSUFFICIENT_HISTORY", severity="info", dataset="price history",
            message=f"Only {len(bars)} daily bars available; the 20-day moving average was not computed.",
        ))

    hi = max(bars, key=lambda b: b.high)
    lo = min(bars, key=lambda b: b.low)
    reg.add("C", label=f"Highest intraday high in window (on {hi.date.isoformat()})", value=hi.high,
            unit=cur, display=_money(hi.high, cur), method="max of daily highs", **common)
    reg.add("C", label=f"Lowest intraday low in window (on {lo.date.isoformat()})", value=lo.low,
            unit=cur, display=_money(lo.low, cur), method="min of daily lows", **common)

    if len(bars) >= 3:
        returns = [(b.close / a.close - 1) * 100 for a, b in zip(bars, bars[1:])]
        vol = statistics.stdev(returns)
        reg.add("C", label="Standard deviation of daily close-to-close returns", value=round(vol, 2),
                unit="%", display=f"{vol:.2f}%", method="sample stdev of daily % returns", **common)

    avg_volume = round(statistics.fmean(b.volume for b in bars))
    reg.add("C", label="Average daily volume in window", value=avg_volume, unit="shares",
            display=f"{avg_volume:,} shares", method="mean of daily volumes", **common)


def build_facts(bundle: DataBundle, settings: Settings) -> tuple[FactRegistry, list[Flag]]:
    reg = FactRegistry()
    flags: list[Flag] = []
    quote, history, ann = bundle.quote, bundle.history, bundle.announcements

    if quote is not None:
        _add_bar_facts(reg, quote.data.bar, quote.data.currency, quote.source.source_id,
                       quote.observed_at, feed="quote feed")
    elif history is not None:
        last = history.data.bars[-1]
        _add_bar_facts(reg, last, history.data.currency, history.source.source_id,
                       history.observed_at, feed="history feed")

    if quote is not None and history is not None:
        q = quote.data.bar
        match = next((b for b in history.data.bars if b.date == q.date), None)
        if match is not None:
            diff_pct = abs(match.close - q.close) / match.close * 100
            if diff_pct > settings.price_disagreement_pct:
                cur = quote.data.currency
                reg.add("P", kind="reported",
                        label=f"Close price on {q.date.isoformat()} (history feed)",
                        value=match.close, unit=cur, display=_money(match.close, cur),
                        source_id=history.source.source_id, observed_at=history.observed_at)
                flags.append(Flag(
                    code="PRICE_SOURCES_DISAGREE", severity="critical", dataset="price",
                    message=(
                        f"The quote feed reports a {q.date.isoformat()} close of {_money(q.close, cur)} "
                        f"but the history feed reports {_money(match.close, cur)} for the same day "
                        f"({diff_pct:.2f}% apart). Both values are shown and neither was chosen; "
                        "price-based conclusions are unreliable until this is resolved."
                    ),
                ))

    if history is not None:
        _add_history_metrics(reg, bundle, flags)

    if ann is not None:
        items = sorted(ann.data.items, key=lambda i: i.published_at)
        period = ann.query.get("period", "?")
        for item in items[-MAX_ANNOUNCEMENT_FACTS:]:
            reg.add("N", kind="reported",
                    label=f"Announcement on {item.published_at.date().isoformat()} ({item.category})",
                    value=item.title, display=item.title, detail=item.summary or None,
                    source_id=item.url or ann.source.source_id, observed_at=ann.observed_at)
        reg.add("C", kind="computed", label=f"Number of announcements in the last {period}",
                value=len(items), unit="announcements", display=str(len(items)),
                method=f"count of items returned by get_announcements (the latest "
                       f"{MAX_ANNOUNCEMENT_FACTS} are listed as facts)",
                source_id=ann.source.source_id, observed_at=ann.observed_at)
        if not items:
            flags.append(Flag(
                code="NO_ANNOUNCEMENTS", severity="info", dataset="company announcements",
                message=f"The announcements feed returned no items for the last {period}.",
            ))

    return reg, flags
