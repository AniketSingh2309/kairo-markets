"""Option maths: Black-Scholes prices and Greeks, implied volatility, and OI build-up labels (pure).

NSE index and stock options are European-style and cash-settled, so plain Black-Scholes applies.
Dividends aren't modelled (fine for indices; slightly off for high-dividend stocks near ex-date).
"""

from __future__ import annotations

import datetime as dt
import math

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
EXPIRY_TIME = dt.time(15, 30)
DEFAULT_RATE = 0.065                     # approx. Indian 91-day T-bill yield
MIN_T = 1 / (365 * 24 * 60)              # one minute, so expiry-day maths stays finite


def _n(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _N(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def years_to_expiry(expiry: dt.date, now: dt.datetime) -> float:
    end = dt.datetime.combine(expiry, EXPIRY_TIME, tzinfo=IST)
    return max(MIN_T, (end - now).total_seconds() / (365 * 86400))


def bs_price(kind: str, S: float, K: float, T: float, sigma: float, r: float = DEFAULT_RATE) -> float:
    if sigma <= 0 or T <= 0:
        return max(0.0, S - K) if kind == "CE" else max(0.0, K - S)
    sq = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r + sigma * sigma / 2) * T) / sq
    d2 = d1 - sq
    if kind == "CE":
        return S * _N(d1) - K * math.exp(-r * T) * _N(d2)
    return K * math.exp(-r * T) * _N(-d2) - S * _N(-d1)


def greeks(kind: str, S: float, K: float, T: float, sigma: float, r: float = DEFAULT_RATE) -> dict[str, float]:
    """Delta, gamma, theta (per calendar day) and vega (per 1 point of IV)."""
    sq = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r + sigma * sigma / 2) * T) / sq
    d2 = d1 - sq
    pdf, disc = _n(d1), K * math.exp(-r * T)
    gamma = pdf / (S * sq)
    vega = S * pdf * math.sqrt(T) / 100
    decay = -S * pdf * sigma / (2 * math.sqrt(T))
    if kind == "CE":
        delta, theta = _N(d1), (decay - r * disc * _N(d2)) / 365
    else:
        delta, theta = _N(d1) - 1, (decay + r * disc * _N(-d2)) / 365
    return {"delta": round(delta, 4), "gamma": round(gamma, 6), "theta": round(theta, 2), "vega": round(vega, 2)}


def implied_vol(kind: str, price: float, S: float, K: float, T: float, r: float = DEFAULT_RATE) -> float | None:
    """Volatility that reproduces the market price (bisection), or None if the price is below intrinsic value."""
    intrinsic = max(0.0, S - K * math.exp(-r * T)) if kind == "CE" else max(0.0, K * math.exp(-r * T) - S)
    if price is None or price <= intrinsic + 1e-6:
        return None
    lo, hi = 1e-4, 5.0
    if bs_price(kind, S, K, T, hi, r) < price:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if bs_price(kind, S, K, T, mid, r) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def side_greeks(kind: str, side: dict | None, S: float | None, K: float, T: float, r: float = DEFAULT_RATE) -> dict | None:
    """Greeks for one chain row side, using NSE's IV or (when it's 0) the IV implied by the last price."""
    if not side or not S or S <= 0 or K <= 0:
        return None
    iv = (side.get("iv") or 0) / 100
    source = "nse"
    if iv <= 0:
        iv = implied_vol(kind, side.get("ltp") or 0, S, K, T, r) or 0
        source = "price"
    if iv <= 0:
        return None
    return {**greeks(kind, S, K, T, iv, r), "iv": round(iv * 100, 2), "iv_source": source}


# OI build-up: what the day's price and open-interest moves together suggest about positioning.
BUILDUP = {
    (True, True): ("Long build-up", "up"),       # price up, OI up: new buying
    (False, True): ("Short build-up", "down"),   # price down, OI up: new selling
    (True, False): ("Short covering", "up"),     # price up, OI down: shorts closing
    (False, False): ("Long unwinding", "down"),  # price down, OI down: longs closing
}


def buildup(price_change: float | None, oi_change: float | None) -> dict | None:
    if not price_change or not oi_change:
        return None
    label, tone = BUILDUP[(price_change > 0, oi_change > 0)]
    return {"label": label, "tone": tone}
