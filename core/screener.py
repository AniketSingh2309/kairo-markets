"""Stock screener: per-symbol metrics from one year of daily bars, plus scan universes."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence

from pydantic import BaseModel

from core import technicals as ta
from tools.models import PriceBar

# Extra universes registered in code: {key: (label, [symbols])}. Empty in the app, where every
# universe is built from an official list (core/universes.py); tests register small ones here.
UNIVERSES: dict[str, tuple[str, list[str]]] = {}


class ScreenerRow(BaseModel):
    symbol: str
    name: str | None
    currency: str
    price: float
    as_of: str
    chg_1d: float | None
    chg_1w: float | None
    chg_1m: float | None
    chg_3m: float | None
    chg_6m: float | None
    chg_1y: float | None
    rsi14: float | None
    above_sma50: bool | None
    above_sma200: bool | None
    golden_cross: bool | None
    from_52w_high: float | None
    from_52w_low: float | None
    volatility: float | None
    avg_volume: int | None


def _chg(closes: Sequence[float], back: int) -> float | None:
    if len(closes) <= back or closes[-1 - back] == 0:
        return None
    return round((closes[-1] / closes[-1 - back] - 1) * 100, 2)


def screen_row(symbol: str, name: str | None, currency: str, bars: Sequence[PriceBar]) -> ScreenerRow | None:
    if len(bars) < 2:
        return None
    closes = [b.close for b in bars]
    last = closes[-1]
    sma50, sma200 = ta.sma(closes, 50)[-1], ta.sma(closes, 200)[-1]
    rsi = ta.rsi(closes, 14)[-1]
    window = bars[-252:]
    hi, lo = max(b.high for b in window), min(b.low for b in window)
    rets = [closes[i] / closes[i - 1] - 1 for i in range(max(1, len(closes) - 20), len(closes))]
    vol = statistics.stdev(rets) * math.sqrt(252) * 100 if len(rets) >= 5 else None
    vols = [b.volume for b in bars[-20:]]
    return ScreenerRow(
        symbol=symbol, name=name, currency=currency, price=last, as_of=bars[-1].date.isoformat(),
        chg_1d=_chg(closes, 1), chg_1w=_chg(closes, 5), chg_1m=_chg(closes, 21), chg_3m=_chg(closes, 63),
        chg_6m=_chg(closes, 126), chg_1y=_chg(closes, 250),
        rsi14=round(rsi, 1) if rsi is not None else None,
        above_sma50=last > sma50 if sma50 else None, above_sma200=last > sma200 if sma200 else None,
        golden_cross=sma50 > sma200 if sma50 and sma200 else None,
        from_52w_high=round((last / hi - 1) * 100, 2) if hi else None,
        from_52w_low=round((last / lo - 1) * 100, 2) if lo else None,
        volatility=round(vol, 1) if vol is not None else None,
        avg_volume=int(statistics.fmean(vols)) if vols and any(vols) else None)
