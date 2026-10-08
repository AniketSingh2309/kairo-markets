"""Money-weighted return (XIRR)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

CashFlow = tuple[dt.date, float]  # (date, amount): money out of pocket < 0, money back > 0


def xnpv(rate: float, flows: Sequence[CashFlow]) -> float:
    t0 = min(d for d, _ in flows)
    return sum(cf / (1 + rate) ** ((d - t0).days / 365.0) for d, cf in flows)


def xirr(flows: Sequence[CashFlow], *, tol: float = 1e-9, max_iter: int = 100) -> float | None:
    """Annualised internal rate of return for irregular cash flows (Excel XIRR convention).

    Returns None when undefined: fewer than two flows, flows all of one sign, or
    every flow on the same day.
    """
    flows = [(d, float(cf)) for d, cf in flows if cf]
    if len(flows) < 2 or all(cf > 0 for _, cf in flows) or all(cf < 0 for _, cf in flows):
        return None
    t0 = min(d for d, _ in flows)
    years = [((d - t0).days / 365.0, cf) for d, cf in flows]
    if all(t == 0 for t, _ in years):
        return None

    def f(r: float) -> float:
        return sum(cf / (1 + r) ** t for t, cf in years)

    def df(r: float) -> float:
        return sum(-t * cf / (1 + r) ** (t + 1) for t, cf in years)

    # Newton-Raphson from a sensible guess.
    r = 0.1
    for _ in range(max_iter):
        try:
            fr, dfr = f(r), df(r)
        except (OverflowError, ZeroDivisionError):
            break
        if dfr == 0:
            break
        step = fr / dfr
        nxt = r - step
        if nxt <= -0.999999:
            nxt = (r - 0.999999) / 2  # stay in the domain
        if abs(nxt - r) < tol:
            return nxt
        r = nxt

    # Fallback: bisection over a wide bracket.
    lo, hi = -0.999999, 1.0
    try:
        f_lo = f(lo)
        while f_lo * f(hi) > 0 and hi < 1e6:
            hi *= 10
        if f_lo * f(hi) > 0:
            return None
        for _ in range(300):
            mid = (lo + hi) / 2
            f_mid = f(mid)
            if abs(f_mid) < tol or (hi - lo) < tol:
                return mid
            if f_lo * f_mid < 0:
                hi = mid
            else:
                lo, f_lo = mid, f_mid
        return (lo + hi) / 2
    except (OverflowError, ZeroDivisionError):
        return None
