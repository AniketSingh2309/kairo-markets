"""Screener query language, like Screener.in / Chartink: ``roe > 15 AND pe < 25 AND close > sma200``.

Grammar (case-insensitive keywords; ``&&``, ``||`` and ``!`` work too)::

    expr  := or
    or    := and ("OR" and)*
    and   := not ("AND" not)*
    not   := "NOT" not | cmp
    cmp   := sum (("<" | "<=" | ">" | ">=" | "=" | "==" | "!=") sum)?
    sum   := term (("+" | "-") term)*
    term  := unary (("*" | "/") unary)*
    unary := "-" unary | atom
    atom  := NUMBER | FIELD | "(" expr ")"

It is parsed into a small tree and evaluated by code; nothing is ever passed to ``eval``. Evaluation is
three-valued: a missing field is *unknown*, so ``roe > 15`` is unknown when ROE isn't available, and an
unknown result counts as no match. The same logic lets the screener skip fetching fundamentals for a stock
that the cheap (price-based) conditions already rule out.
"""

from __future__ import annotations

import difflib
import math
import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from core import technicals as ta

# name -> (group, unit, description). Groups decide what has to be fetched.
FIELDS: dict[str, tuple[str, str, str]] = {
    # price and technicals (from daily candles; cheap)
    "close": ("technical", "₹", "Last close"), "price": ("technical", "₹", "Last close (same as close)"),
    "chg_1d": ("technical", "%", "Change over 1 day"), "chg_1w": ("technical", "%", "Change over 1 week"),
    "chg_1m": ("technical", "%", "Change over 1 month"), "chg_3m": ("technical", "%", "Change over 3 months"),
    "chg_6m": ("technical", "%", "Change over 6 months"), "chg_1y": ("technical", "%", "Change over 1 year"),
    "rsi": ("technical", "", "RSI (14)"), "sma20": ("technical", "₹", "20-day simple moving average"),
    "sma50": ("technical", "₹", "50-day simple moving average"), "sma200": ("technical", "₹", "200-day simple moving average"),
    "ema20": ("technical", "₹", "20-day exponential moving average"), "macd_hist": ("technical", "", "MACD histogram (12, 26, 9)"),
    "adx": ("technical", "", "ADX (14): trend strength, above 25 = trending"), "atr_pct": ("technical", "%", "ATR (14) as % of price"),
    "vol_x": ("technical", "×", "Today's volume vs its 20-day average"), "avg_volume": ("technical", "", "20-day average volume"),
    "high_52w": ("technical", "₹", "52-week high"), "low_52w": ("technical", "₹", "52-week low"),
    "from_52w_high": ("technical", "%", "Distance from the 52-week high (negative = below)"),
    "from_52w_low": ("technical", "%", "Distance above the 52-week low"),
    "volatility": ("technical", "%", "Annualised volatility (20 days)"),
    "golden_cross": ("technical", "yes/no", "50-day average above the 200-day"),
    # India (NSE daily files; cheap)
    "delivery_pct": ("india", "%", "Delivery % in the last session"), "delivery_x": ("india", "×", "Delivered quantity vs its recent average"),
    "turnover_cr": ("india", "₹ cr", "Turnover in the last session"), "asm": ("india", "yes/no", "Under ASM surveillance"),
    "gsm": ("india", "yes/no", "Under GSM surveillance"), "fo_ban": ("india", "yes/no", "In the F&O ban"),
    # fundamentals (Yahoo; fetched per stock, only when the query needs them)
    "mcap_cr": ("fundamental", "₹ cr", "Market cap, in crore"), "pe": ("fundamental", "×", "Price / earnings"),
    "pb": ("fundamental", "×", "Price / book"), "ps": ("fundamental", "×", "Price / sales"), "ev_ebitda": ("fundamental", "×", "EV / EBITDA"),
    "roe": ("fundamental", "%", "Return on equity"), "net_margin": ("fundamental", "%", "Net profit margin"),
    "operating_margin": ("fundamental", "%", "Operating margin"), "debt_to_equity": ("fundamental", "×", "Debt / equity"),
    "revenue_growth": ("fundamental", "%", "Revenue growth (year on year)"), "earnings_growth": ("fundamental", "%", "Earnings growth (year on year)"),
    "dividend_yield": ("fundamental", "%", "Dividend yield"), "beta": ("fundamental", "", "Beta"), "eps": ("fundamental", "₹", "Earnings per share"),
}

EXAMPLES = [
    ("Quality at a fair price", "roe > 15 AND debt_to_equity < 0.5 AND pe < 30"),
    ("Uptrend, near highs", "close > sma50 AND sma50 > sma200 AND from_52w_high > -5"),
    ("Oversold in an uptrend", "rsi < 35 AND close > sma200"),
    ("Volume breakout", "vol_x > 2 AND chg_1d > 3"),
    ("High-delivery buying", "delivery_pct > 60 AND delivery_x > 1.5 AND chg_1d > 0"),
    ("Strong trend", "adx > 25 AND close > ema20 AND macd_hist > 0"),
    ("Growth stocks", "revenue_growth > 15 AND earnings_growth > 15 AND mcap_cr > 5000"),
    ("Dividend payers", "dividend_yield > 2 AND pe < 25 AND NOT asm"),
]


class QueryError(ValueError):
    pass


_TOKEN = re.compile(r"\s*(?:(\d+(?:\.\d+)?(?:e[+-]?\d+)?)|([A-Za-z_][A-Za-z0-9_]*)|(<=|>=|==|!=|&&|\|\||[<>=!()+\-*/]))", re.I)


def _tokens(text: str) -> list[tuple[str, str]]:
    out, pos, text = [], 0, text.strip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise QueryError(f"I don't understand '{text[pos:pos + 12]}' (position {pos + 1})")
        num, name, op = m.groups()
        if num:
            out.append(("num", num))
        elif name:
            low = name.lower()
            out.append(("op", low) if low in ("and", "or", "not") else ("name", low))
        else:
            out.append(("op", {"&&": "and", "||": "or", "!": "not", "=": "=="}.get(op, op)))
        pos = m.end()
    return out


@dataclass
class Query:
    text: str
    tree: Any
    fields: set[str]

    @property
    def groups(self) -> set[str]:
        return {FIELDS[f][0] for f in self.fields}

    def evaluate(self, row: dict[str, Any]) -> bool | None:
        v = _eval(self.tree, row)
        return None if v is None else bool(v)


def parse(text: str) -> Query:
    if not text or not text.strip():
        raise QueryError("Write a condition, e.g. roe > 15 AND pe < 25")
    if len(text) > 600:
        raise QueryError("That query is too long (600 characters at most)")
    toks, i, used = _tokens(text), 0, set()

    def peek() -> tuple[str, str] | None:
        return toks[i] if i < len(toks) else None

    def take(kind: str | None = None, value: str | None = None) -> tuple[str, str]:
        nonlocal i
        t = peek()
        if t is None or (kind and t[0] != kind) or (value and t[1] != value):
            want = value or kind or "more"
            raise QueryError(f"Expected {want!r} {'at the end' if t is None else f'before {t[1]!r}'}")
        i += 1
        return t

    def or_():
        node = and_()
        while peek() == ("op", "or"):
            take(); node = ("or", node, and_())
        return node

    def and_():
        node = not_()
        while peek() == ("op", "and"):
            take(); node = ("and", node, not_())
        return node

    def not_():
        if peek() == ("op", "not"):
            take(); return ("not", not_())
        return cmp()

    def cmp():
        left = sum_()
        t = peek()
        if t and t[0] == "op" and t[1] in ("<", "<=", ">", ">=", "==", "!="):
            take(); return ("cmp", t[1], left, sum_())
        return left

    def sum_():
        node = term()
        while (t := peek()) and t[0] == "op" and t[1] in "+-":
            take(); node = ("bin", t[1], node, term())
        return node

    def term():
        node = unary()
        while (t := peek()) and t[0] == "op" and t[1] in "*/":
            take(); node = ("bin", t[1], node, unary())
        return node

    def unary():
        if peek() == ("op", "-"):
            take(); return ("neg", unary())
        return atom()

    def atom():
        t = peek()
        if t is None:
            raise QueryError("The query ends too early")
        if t[0] == "num":
            take(); return ("num", float(t[1]))
        if t[0] == "name":
            take()
            if t[1] not in FIELDS:
                near = difflib.get_close_matches(t[1], FIELDS, n=3)
                raise QueryError(f"Unknown field '{t[1]}'" + (f". Did you mean {', '.join(near)}?" if near else ". See the list of fields."))
            used.add(t[1]); return ("field", t[1])
        if t == ("op", "("):
            take(); node = or_(); take("op", ")"); return node
        raise QueryError(f"Unexpected '{t[1]}'")

    tree = or_()
    if i != len(toks):
        raise QueryError(f"Unexpected '{toks[i][1]}' after a complete condition")
    return Query(text.strip(), tree, used)


def _num(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    return float(v) if isinstance(v, (int, float)) and math.isfinite(v) else None


def _eval(node: Any, row: dict[str, Any]) -> Any:
    kind = node[0]
    if kind == "num":
        return node[1]
    if kind == "field":
        return row.get(node[1])
    if kind == "neg":
        v = _num(_eval(node[1], row)); return None if v is None else -v
    if kind == "bin":
        a, b = _num(_eval(node[2], row)), _num(_eval(node[3], row))
        if a is None or b is None:
            return None
        if node[1] == "/":
            return None if b == 0 else a / b
        return {"+": a + b, "-": a - b, "*": a * b}[node[1]]
    if kind == "cmp":
        a, b = _num(_eval(node[2], row)), _num(_eval(node[3], row))
        if a is None or b is None:
            return None
        return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b, "==": abs(a - b) < 1e-9, "!=": abs(a - b) >= 1e-9}[node[1]]
    if kind == "not":
        v = _truth(_eval(node[1], row)); return None if v is None else not v
    a = _truth(_eval(node[1], row))
    if kind == "and":
        if a is False:
            return False
        b = _truth(_eval(node[2], row))
        return False if b is False else (None if a is None or b is None else True)
    if a is True:  # or
        return True
    b = _truth(_eval(node[2], row))
    return True if b is True else (None if a is None or b is None else False)


def _truth(v: Any) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    n = _num(v)
    return None if n is None else n != 0


# ------------------------------------------------------------------------------------------ field values


def technical_fields(bars: Sequence[Any]) -> dict[str, Any]:
    """Every technical field from daily bars (oldest first; each with open/high/low/close/volume)."""
    if len(bars) < 2:
        return {}
    closes, highs, lows = [b.close for b in bars], [b.high for b in bars], [b.low for b in bars]
    vols = [b.volume or 0 for b in bars]
    last = closes[-1]
    chg = lambda back: round((last / closes[-1 - back] - 1) * 100, 2) if len(closes) > back and closes[-1 - back] else None  # noqa: E731
    lastv = lambda s: s[-1] if s and s[-1] is not None else None  # noqa: E731
    sma20, sma50, sma200, ema20 = lastv(ta.sma(closes, 20)), lastv(ta.sma(closes, 50)), lastv(ta.sma(closes, 200)), lastv(ta.ema(closes, 20))
    _, _, hist = ta.macd(closes)
    adx = lastv(ta.adx(highs, lows, closes)[0]) if len(closes) > 30 else None
    atr = lastv(ta.atr(highs, lows, closes)) if len(closes) > 15 else None
    window = bars[-252:]
    hi, lo = max(b.high for b in window), min(b.low for b in window)
    rets = [closes[i] / closes[i - 1] - 1 for i in range(max(1, len(closes) - 20), len(closes)) if closes[i - 1]]
    avg20 = statistics.fmean(vols[-21:-1]) if len(vols) > 21 and any(vols[-21:-1]) else None
    r = lambda v, d=2: round(v, d) if v is not None else None  # noqa: E731
    return {
        "close": last, "price": last, "chg_1d": chg(1), "chg_1w": chg(5), "chg_1m": chg(21), "chg_3m": chg(63), "chg_6m": chg(126), "chg_1y": chg(250),
        "rsi": r(lastv(ta.rsi(closes, 14)), 1), "sma20": r(sma20), "sma50": r(sma50), "sma200": r(sma200), "ema20": r(ema20),
        "macd_hist": r(lastv(hist), 3), "adx": r(adx, 1), "atr_pct": r(atr / last * 100 if atr and last else None),
        "vol_x": r(vols[-1] / avg20 if avg20 else None), "avg_volume": int(avg20) if avg20 else None,
        "high_52w": hi, "low_52w": lo, "from_52w_high": r((last / hi - 1) * 100 if hi else None), "from_52w_low": r((last / lo - 1) * 100 if lo else None),
        "volatility": r(statistics.stdev(rets) * math.sqrt(252) * 100 if len(rets) >= 5 else None, 1),
        "golden_cross": (sma50 > sma200) if sma50 and sma200 else None,
    }


def fundamental_fields(f: Any) -> dict[str, Any]:
    """From a ``tools.models.Fundamentals``: ratios in %, market cap in crore."""
    pct = lambda v: round(v * 100, 2) if v is not None else None  # noqa: E731
    # Yahoo reports dividend yield as a fraction for most tickers but already in % for some; > 1 means %.
    dy = f.dividend_yield
    return {"mcap_cr": round(f.market_cap / 1e7, 1) if f.market_cap else None, "pe": f.pe, "pb": f.pb, "ps": f.ps, "ev_ebitda": f.ev_ebitda,
            "roe": pct(f.roe), "net_margin": pct(f.net_margin), "operating_margin": pct(f.operating_margin), "debt_to_equity": f.debt_to_equity,
            "revenue_growth": pct(f.revenue_growth), "earnings_growth": pct(f.earnings_growth),
            "dividend_yield": None if dy is None else round(dy if dy > 1 else dy * 100, 2), "beta": f.beta, "eps": f.eps}
