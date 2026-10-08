"""Trading screens with an honest track record.

Each screen is a textbook technical *event* (a crossover or a break, not a persistent
state, so one move isn't counted twenty times). For every screen we report:

* which stocks in the universe triggered it in the last few sessions, and
* how it has actually done across that universe's recent history: of every past
  trigger, how often the price was higher (bullish screens) / lower (bearish screens)
  ``horizon`` sessions later, the average move, and the base rate for *any* day --
  so "61% right" can be compared with "the market was up 55% of the time anyway".
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from core import technicals as ta
from core.forecast import wilson_interval
from tools.models import PriceBar

Bias = Literal["bullish", "bearish"]
RECENT_SESSIONS = 3      # a screen "matches now" if it triggered in the last N sessions
MIN_EVENTS = 15          # fewer past triggers than this: no verdict


@dataclass
class Series:
    closes: list[float]
    highs: list[float]
    lows: list[float]
    rsi: list
    macd: list
    signal: list
    sma50: list
    sma200: list


def series_of(bars: Sequence[PriceBar]) -> Series:
    closes = [b.close for b in bars]
    macd, sig, _ = ta.macd(closes)
    return Series(closes=closes, highs=[b.high for b in bars], lows=[b.low for b in bars], rsi=ta.rsi(closes, 14),
                  macd=macd, signal=sig, sma50=ta.sma(closes, 50), sma200=ta.sma(closes, 200))


def _cross_up(a: list, b: list, i: int) -> bool:
    return i >= 1 and None not in (a[i], b[i], a[i - 1], b[i - 1]) and a[i] > b[i] and a[i - 1] <= b[i - 1]


def _cross_down(a: list, b: list, i: int) -> bool:
    return i >= 1 and None not in (a[i], b[i], a[i - 1], b[i - 1]) and a[i] < b[i] and a[i - 1] >= b[i - 1]


def _breakout(s: Series, i: int) -> bool:
    if i < 21:
        return False
    return s.closes[i] > max(s.highs[i - 20:i]) and s.closes[i - 1] <= max(s.highs[i - 21:i - 1])


def _breakdown(s: Series, i: int) -> bool:
    if i < 21:
        return False
    return s.closes[i] < min(s.lows[i - 20:i]) and s.closes[i - 1] >= min(s.lows[i - 21:i - 1])


def _rsi_cross(level: float, up: bool) -> Callable[[Series, int], bool]:
    def test(s: Series, i: int) -> bool:
        if i < 1 or s.rsi[i] is None or s.rsi[i - 1] is None:
            return False
        return s.rsi[i] > level >= s.rsi[i - 1] if up else s.rsi[i] < level <= s.rsi[i - 1]
    return test


@dataclass(frozen=True)
class Screen:
    key: str
    name: str
    bias: Bias
    description: str
    test: Callable[[Series, int], bool]


SCREENS: tuple[Screen, ...] = (
    Screen("breakout", "20-day breakout", "bullish",
           "Closed above its highest high of the previous 20 sessions.", _breakout),
    Screen("breakdown", "20-day breakdown", "bearish",
           "Closed below its lowest low of the previous 20 sessions.", _breakdown),
    Screen("macd_bull", "MACD bullish crossover", "bullish",
           "MACD line crossed above its signal line.", lambda s, i: _cross_up(s.macd, s.signal, i)),
    Screen("macd_bear", "MACD bearish crossover", "bearish",
           "MACD line crossed below its signal line.", lambda s, i: _cross_down(s.macd, s.signal, i)),
    Screen("rsi_oversold", "RSI oversold", "bullish",
           "RSI(14) dropped below 30 — textbook reading: due a bounce.", _rsi_cross(30, up=False)),
    Screen("rsi_overbought", "RSI overbought", "bearish",
           "RSI(14) rose above 70 — textbook reading: due a pullback.", _rsi_cross(70, up=True)),
    Screen("golden_cross", "Golden cross", "bullish",
           "50-day average crossed above the 200-day average.", lambda s, i: _cross_up(s.sma50, s.sma200, i)),
    Screen("death_cross", "Death cross", "bearish",
           "50-day average crossed below the 200-day average.", lambda s, i: _cross_down(s.sma50, s.sma200, i)),
)


class ScreenMatch(BaseModel):
    symbol: str
    name: str | None
    date: str
    sessions_ago: int
    price: float


class TrackRecord(BaseModel):
    horizon: int
    events: int
    hit_rate: float | None
    ci_low: float | None
    ci_high: float | None
    avg_return_pct: float | None
    base_rate: float | None
    edge: float | None
    verdict: Literal["beats_base_rate", "no_edge", "worse_than_base_rate", "too_few_events"]


class ScreenResult(BaseModel):
    key: str
    name: str
    bias: Bias
    description: str
    matches: list[ScreenMatch]
    track: TrackRecord


def run_screens(histories: dict[str, tuple[str | None, Sequence[PriceBar]]], horizon: int = 10) -> list[ScreenResult]:
    """``histories``: symbol -> (name, daily bars oldest-first)."""
    prepared = {sym: (name, bars, series_of(bars)) for sym, (name, bars) in histories.items() if len(bars) >= 30}

    # Base rates: share of all (symbol, day) windows that rose / fell over the horizon.
    up = down = total = 0
    for _, _, s in prepared.values():
        for i in range(len(s.closes) - horizon):
            fwd = s.closes[i + horizon] / s.closes[i] - 1
            total += 1
            up += fwd > 0
            down += fwd < 0
    base_up = up / total if total else None
    base_down = down / total if total else None

    results: list[ScreenResult] = []
    for sc in SCREENS:
        hits = events = 0
        rets: list[float] = []
        matches: list[ScreenMatch] = []
        for sym, (name, bars, s) in prepared.items():
            n = len(s.closes)
            for i in range(1, n):
                if not sc.test(s, i):
                    continue
                if i + horizon < n:
                    fwd = s.closes[i + horizon] / s.closes[i] - 1
                    events += 1
                    rets.append(fwd * 100)
                    hits += (fwd > 0) if sc.bias == "bullish" else (fwd < 0)
                ago = n - 1 - i
                if ago < RECENT_SESSIONS:
                    matches.append(ScreenMatch(symbol=sym, name=name, date=bars[i].date.isoformat(),
                                               sessions_ago=ago, price=bars[-1].close))
        base = base_up if sc.bias == "bullish" else base_down
        if events < MIN_EVENTS or base is None:
            track = TrackRecord(horizon=horizon, events=events, hit_rate=hits / events if events else None,
                                ci_low=None, ci_high=None, avg_return_pct=sum(rets) / len(rets) if rets else None,
                                base_rate=base, edge=None, verdict="too_few_events")
        else:
            rate = hits / events
            lo, hi = wilson_interval(hits, events)
            verdict = "beats_base_rate" if lo > base else "worse_than_base_rate" if hi < base else "no_edge"
            track = TrackRecord(horizon=horizon, events=events, hit_rate=round(rate, 4), ci_low=round(lo, 4),
                                ci_high=round(hi, 4), avg_return_pct=round(sum(rets) / len(rets), 3),
                                base_rate=round(base, 4), edge=round(rate - base, 4), verdict=verdict)
        matches.sort(key=lambda m: (m.sessions_ago, m.symbol))
        results.append(ScreenResult(key=sc.key, name=sc.name, bias=sc.bias, description=sc.description,
                                    matches=matches, track=track))
    return results
