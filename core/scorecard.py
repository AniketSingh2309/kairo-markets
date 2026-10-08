"""Stock scorecard: how a stock compares with its industry peers on five measures (pure functions).

Each measure is the average percentile of its metrics among the stock and its peers, 0-100, where
higher is always better for the investor (cheaper valuation, lower risk). Loss-making companies have
no meaningful P/E, so negative ratios are left out rather than ranked as "cheap".
"""

from __future__ import annotations

import statistics
from typing import Any

# (key, label, [(metric, +1 higher is better / -1 lower is better)], words for good / average / weak)
DIMENSIONS: list[tuple[str, str, list[tuple[str, int]], tuple[str, str, str]]] = [
    ("performance", "Performance", [("year_change", 1)], ("High", "Average", "Low")),
    ("valuation", "Valuation", [("pe", -1), ("pb", -1)], ("Attractive", "Fair", "Expensive")),
    ("profitability", "Profitability", [("roe", 1), ("net_margin", 1)], ("High", "Average", "Low")),
    ("growth", "Growth", [("revenue_growth", 1), ("earnings_growth", 1)], ("High", "Average", "Low")),
    ("risk", "Risk", [("beta", -1), ("debt_to_equity", -1)], ("Low risk", "Average", "High risk")),
]
POSITIVE_ONLY = {"pe", "pb", "beta", "debt_to_equity"}  # negative values aren't comparable here
MIN_VALUES = 4                                          # need the stock plus at least three peers with data


def usable(metric: str, value: Any) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    if metric in POSITIVE_ONLY and value <= 0:
        return None
    return float(value)


def percentile(value: float, values: list[float], direction: int) -> float:
    """Share of the group this value beats (ties count half), 0-100, flipped when lower is better."""
    below = sum(1 for v in values if v < value)
    equal = sum(1 for v in values if v == value) - 1
    rank = (below + 0.5 * equal) / max(1, len(values) - 1)
    return round((rank if direction > 0 else 1 - rank) * 100, 1)


def scorecard(target: dict[str, Any], peers: list[dict[str, Any]]) -> dict[str, Any]:
    group = [target, *peers]
    medians: dict[str, float | None] = {}
    dims = []
    for key, label, metrics, words in DIMENSIONS:
        parts, shown = [], []
        for metric, direction in metrics:
            values = [v for v in (usable(metric, row.get(metric)) for row in group) if v is not None]
            mine = usable(metric, target.get(metric))
            medians[metric] = round(statistics.median(values), 4) if values else None
            shown.append({"key": metric, "value": target.get(metric), "median": medians[metric], "peers_with_data": len(values) - (mine is not None)})
            if mine is not None and len(values) >= MIN_VALUES:
                parts.append(percentile(mine, values, direction))
        score = round(sum(parts) / len(parts)) if parts else None
        tone = None if score is None else "good" if score >= 67 else "avg" if score >= 34 else "weak"
        verdict = None if score is None else words[0] if tone == "good" else words[1] if tone == "avg" else words[2]
        dims.append({"key": key, "label": label, "score": score, "tone": tone, "verdict": verdict, "metrics": shown})
    return {"dimensions": dims, "medians": medians, "peer_count": len(peers)}


def closest_by_size(candidates: list[tuple[str, float | None]], size: float | None, n: int) -> list[str]:
    """Up to n candidate symbols nearest in market cap (on a log scale), unknown sizes last."""
    import math
    def dist(c: tuple[str, float | None]) -> float:
        cap = c[1]
        if not cap or not size or cap <= 0 or size <= 0:
            return float("inf")
        return abs(math.log(cap) - math.log(size))
    return [s for s, _ in sorted(candidates, key=dist)[:n]]
