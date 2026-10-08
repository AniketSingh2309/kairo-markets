"""Indian mutual funds from AMFI (Association of Mutual Funds in India) -- free, official data.

* Latest NAV of every scheme: ``https://www.amfiindia.com/spages/NAVAll.txt`` (one ~1.5 MB file).
* Every scheme's NAV on a past date: AMFI's NAV history report (one ~1 MB file per date) --
  used to rank funds within their category over 1/3/5 years.
* A single scheme's full NAV history: ``api.mfapi.in`` (a free mirror of AMFI data).

Funds are addressed as ``MF<scheme code>`` (e.g. ``MF122639``) so the rest of the app --
tool contract, portfolio, health check, tax -- treats them like any other symbol. The provider
implements the same endpoints as the stock providers; NAVs are daily, so "live" is the latest NAV.

AMFI files are semicolon-separated with interleaved section rows (scheme category, fund house).
Columns are located by header name because AMFI has changed the layout before.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from core.mutual_funds import canonical_category
from tools.models import is_mf_symbol, mf_code
from tools.providers.base import NoDataAvailable, ProviderUnavailable, SymbolNotFound

NAVALL_URL = "https://www.amfiindia.com/spages/NAVAll.txt"
HISTORY_URL = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"
MFAPI_URL = "https://api.mfapi.in/mf/{code}"
NAV_TIME_UTC = dt.time(15, 30)  # NAVs are struck at end of day IST (~21:00); dated midnight-free here
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Kairo/1.0"}


@dataclass
class Scheme:
    code: str
    name: str
    amc: str | None
    category: str | None          # canonical, e.g. "Equity Scheme - Flexi Cap Fund" (see canonical_category)
    scheme_type: str | None       # "Open Ended Schemes" / "Close Ended Schemes" / "Interval Fund Schemes"
    plan: str | None              # "Direct" / "Regular" (best effort)
    option: str | None            # "Growth" / "IDCW" (best effort)
    isin: str | None
    nav: float | None
    nav_date: dt.date | None
    raw_category: str | None = None  # exactly as AMFI published it


def _parse_date(value: str) -> dt.date | None:
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _num(value: str) -> float | None:
    try:
        v = float(value.replace(",", "").strip())
        return v if v > 0 else None
    except ValueError:
        return None


def _plan_option(name: str, plan: str | None, option: str | None) -> tuple[str | None, str | None]:
    low = name.lower()
    plan = (plan or "").strip() or ("Direct" if "direct" in low else "Regular" if "regular" in low else None)
    plan = "Direct" if plan and "direct" in plan.lower() else "Regular" if plan and "regular" in plan.lower() else plan
    opt = (option or "").strip()
    text = (opt or low).lower()
    if "growth" in text:
        opt = "Growth"
    elif any(k in text for k in ("idcw", "dividend", "payout", "reinvest", "bonus")):
        opt = "IDCW"
    return plan or None, opt or None


def _split_sections(text: str):
    """Yield (header_columns, category, scheme_type, amc, row_columns) for every data row."""
    lines = [l.strip() for l in text.lstrip("﻿").splitlines()]
    header: list[str] = []
    category = scheme_type = amc = None
    for line in lines:
        if not line:
            continue
        if line.lower().startswith("scheme code;"):
            header = [h.strip().lower() for h in line.split(";")]
            continue
        cols = line.split(";")
        if len(cols) >= 5 and cols[0].strip().isdigit():
            yield header, category, scheme_type, amc, [c.strip() for c in cols]
            continue
        if "Scheme" in line and "(" in line and line.endswith(")"):
            scheme_type, _, rest = line.partition("(")
            category = rest[:-1].strip()
            scheme_type = scheme_type.strip()
            continue
        if ";" not in line:
            amc = line


def _col(header: list[str], row: list[str], *names: str) -> str:
    for name in names:
        for i, h in enumerate(header):
            if h.startswith(name) and i < len(row):
                return row[i]
    return ""


def parse_navall(text: str) -> dict[str, Scheme]:
    schemes: dict[str, Scheme] = {}
    for header, category, scheme_type, amc, row in _split_sections(text):
        name = _col(header, row, "scheme name", "nav name")
        plan, option = _plan_option(name, _col(header, row, "plan"), _col(header, row, "option"))
        isin = _col(header, row, "isin div payout", "isin growth")
        schemes[row[0]] = Scheme(code=row[0], name=name, amc=amc, category=canonical_category(category, name),
                                 raw_category=category, scheme_type=scheme_type,
                                 plan=plan, option=option, isin=None if isin in ("", "-") else isin,
                                 nav=_num(_col(header, row, "net asset value")),
                                 nav_date=_parse_date(_col(header, row, "date")))
    return schemes


def parse_history_report(text: str) -> dict[str, float]:
    """AMFI NAV-history report for one date -> {scheme code: NAV}."""
    out: dict[str, float] = {}
    for header, _, _, _, row in _split_sections(text):
        nav = _num(_col(header, row, "net asset value"))
        if nav is not None:
            out[row[0]] = nav
    return out


@dataclass
class _Cached:
    at: float
    value: Any


@dataclass
class AmfiData:
    """Fetching + caching of the three AMFI sources. Thread-safe; one fetch per key at a time.

    With ``cache_dir`` set, downloads also go to disk so a restart doesn't re-fetch megabytes: the
    scheme list and NAV histories for their TTL, and past-date NAV reports for good (they never change).
    """

    client: httpx.Client
    cache_dir: Path | None = None
    catalog_ttl_s: float = 6 * 3600
    history_ttl_s: float = 6 * 3600
    snapshot_ttl_s: float = 24 * 3600
    _cache: dict[Any, _Cached] = field(default_factory=dict)
    _locks: dict[Any, threading.Lock] = field(default_factory=dict)
    _guard: threading.Lock = field(default_factory=threading.Lock)

    def _cached(self, key: Any, ttl: float, load: Callable[[], Any]) -> Any:
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit.at < ttl:
            return hit.value
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit.at < ttl:
                return hit.value
            value = load()
            self._cache[key] = _Cached(time.monotonic(), value)
            return value

    # --- disk ------------------------------------------------------------------------------------

    def _disk_read(self, name: str, ttl: float | None) -> Any:
        if self.cache_dir is None:
            return None
        path = self.cache_dir / name
        try:
            if ttl is not None and time.time() - path.stat().st_mtime > ttl:
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _disk_write(self, name: str, value: Any) -> None:
        if self.cache_dir is None:
            return
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_dir / (name + ".part")
            tmp.write_text(json.dumps(value), encoding="utf-8")
            os.replace(tmp, self.cache_dir / name)
        except OSError:
            pass  # the disk cache is only an optimisation

    def _get(self, url: str, **params: Any) -> httpx.Response:
        try:
            r = self.client.get(url, params=params or None)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"AMFI data request failed: {type(exc).__name__}: {exc}") from exc
        if r.status_code >= 500 or r.status_code == 429:
            raise ProviderUnavailable(f"AMFI data source returned HTTP {r.status_code}")
        return r

    def catalog(self) -> dict[str, Scheme]:
        def load() -> dict[str, Scheme]:
            text = self._disk_read("navall.json", self.catalog_ttl_s)
            if not isinstance(text, str):
                text = self._get(NAVALL_URL).text
                fresh = True
            else:
                fresh = False
            schemes = parse_navall(text)
            if not schemes:
                raise ProviderUnavailable("AMFI NAV file parsed to zero schemes (format change?)")
            if fresh:
                self._disk_write("navall.json", text)
            return schemes
        return self._cached("catalog", self.catalog_ttl_s, load)

    def scheme(self, code: str) -> Scheme:
        s = self.catalog().get(code)
        if s is None:
            raise SymbolNotFound(f"no AMFI scheme with code {code}")
        return s

    def history(self, code: str) -> list[tuple[dt.date, float]]:
        """Full NAV history, oldest first."""
        def load() -> list[tuple[dt.date, float]]:
            saved = self._disk_read(f"history_{code}.json", self.history_ttl_s)
            if isinstance(saved, list) and saved:
                return [(dt.date.fromisoformat(d), n) for d, n in saved]
            r = self._get(MFAPI_URL.format(code=code))
            if r.status_code == 404:
                raise SymbolNotFound(f"no NAV history for scheme {code}")
            try:
                rows = r.json().get("data") or []
            except ValueError as exc:
                raise ProviderUnavailable("NAV history response was not JSON") from exc
            series = sorted({d: n for d, n in ((_parse_date(x.get("date", "")), _num(str(x.get("nav", "")))) for x in rows
                                               if isinstance(x, dict)) if d and n}.items())
            if not series:
                raise NoDataAvailable(f"empty NAV history for scheme {code}")
            self._disk_write(f"history_{code}.json", [(d.isoformat(), n) for d, n in series])
            return series
        return self._cached(("history", code), self.history_ttl_s, load)

    snapshot_min_rows: int = 1000

    def snapshot(self, day: dt.date, max_back: int = 7) -> tuple[dt.date, dict[str, float]]:
        """Every scheme's NAV on ``day`` (or the closest earlier weekday with a full report, within ``max_back`` days).

        Weekend reports only list the few schemes that publish weekend NAVs (e.g. some liquid funds), so
        weekends are skipped and a report must be realistically large to count.
        """
        def load() -> tuple[dt.date, dict[str, float]]:
            name = f"snapshot_{day.isoformat()}.json"
            saved = self._disk_read(name, None if day < dt.date.today() else self.snapshot_ttl_s)
            if isinstance(saved, dict) and len(saved.get("navs") or {}) >= self.snapshot_min_rows:
                return dt.date.fromisoformat(saved["date"]), saved["navs"]
            for back in range(max_back + 1):
                d = day - dt.timedelta(days=back)
                if d.weekday() >= 5:
                    continue
                text = self._get(HISTORY_URL, tp="1", frmdt=d.strftime("%d-%b-%Y"), todt=d.strftime("%d-%b-%Y")).text
                navs = parse_history_report(text)
                if len(navs) >= self.snapshot_min_rows:
                    self._disk_write(name, {"date": d.isoformat(), "navs": navs})
                    return d, navs
            raise NoDataAvailable(f"no AMFI NAV report within {max_back} days before {day}")
        return self._cached(("snapshot", day), self.snapshot_ttl_s, load)


class AmfiProvider:
    """Market-data provider for ``MF<code>`` symbols."""

    name = "amfi"

    def __init__(self, *, timeout_s: float = 20.0, client: httpx.Client | None = None, cache_dir: Path | None = None):
        self.data = AmfiData(client or httpx.Client(headers=_HEADERS, timeout=timeout_s, follow_redirects=True),
                             cache_dir=cache_dir)

    # -- helpers ----------------------------------------------------------
    def _series(self, symbol: str) -> tuple[Scheme, list[tuple[dt.date, float]]]:
        code = mf_code(symbol)
        scheme = self.data.scheme(code)
        try:
            series = self.data.history(code)
        except (ProviderUnavailable, NoDataAvailable, SymbolNotFound):
            if scheme.nav is None or scheme.nav_date is None:
                raise
            series = [(scheme.nav_date, scheme.nav)]
        if scheme.nav and scheme.nav_date and series[-1][0] < scheme.nav_date:
            series = [*series, (scheme.nav_date, scheme.nav)]  # AMFI's file can be a day ahead of the mirror
        return scheme, series

    @staticmethod
    def _stamp(day: dt.date) -> str:
        return dt.datetime.combine(day, NAV_TIME_UTC, tzinfo=dt.timezone.utc).isoformat()

    @staticmethod
    def _bar(day: dt.date, nav: float) -> dict[str, Any]:
        return {"date": day.isoformat(), "open": nav, "high": nav, "low": nav, "close": nav, "volume": 0}

    def _meta(self, symbol: str, kind: str, day: dt.date) -> dict[str, Any]:
        code = mf_code(symbol)
        return {"source_id": f"amfi://{kind}/{code}", "url": f"https://www.amfiindia.com/net-asset-value",
                "observed_at": self._stamp(day)}

    # -- provider protocol ----------------------------------------------------
    def fetch_quote(self, symbol: str, on_date: dt.date | None) -> dict[str, Any]:
        _, series = self._series(symbol)
        if on_date is None:
            day, nav = series[-1]
        else:
            match = [p for p in series if p[0] == on_date]
            if not match:
                raise NoDataAvailable(f"no NAV for {symbol} on {on_date}")
            day, nav = match[0]
        return {"meta": self._meta(symbol, "nav", day), "data": {"symbol": symbol, "currency": "INR", "bar": self._bar(day, nav)}}

    def fetch_price_history(self, symbol: str, lookback_days: int) -> dict[str, Any]:
        scheme, series = self._series(symbol)
        cutoff = series[-1][0] - dt.timedelta(days=lookback_days)
        bars = [self._bar(d, n) for d, n in series if d >= cutoff]
        return {"meta": self._meta(symbol, "history", series[-1][0]),
                "data": {"symbol": symbol, "currency": "INR", "name": scheme.name, "bars": bars}}

    def fetch_daily_snapshot(self, symbol: str) -> dict[str, Any]:
        scheme, series = self._series(symbol)
        day, nav = series[-1]
        prev = series[-2][1] if len(series) > 1 else None
        year = [n for d, n in series if d >= day - dt.timedelta(days=365)]
        return {"meta": self._meta(symbol, "snapshot", day), "data": {
            "symbol": symbol, "name": scheme.name, "exchange": "AMFI", "currency": "INR", "price": nav,
            "previous_close": prev, "volume": None, "avg_volume": None,
            "fifty_two_week_high": max(year), "fifty_two_week_low": min(year),
            "session_date": day.isoformat(), "market_time": self._stamp(day), "closes": [n for _, n in series[-22:]]}}

    def fetch_live(self, symbol: str) -> dict[str, Any]:
        scheme, series = self._series(symbol)
        day, nav = series[-1]
        prev = series[-2][1] if len(series) > 1 else None
        year = [n for d, n in series if d >= day - dt.timedelta(days=365)]
        return {"meta": self._meta(symbol, "nav", day), "data": {
            "symbol": symbol, "name": scheme.name, "exchange": "AMFI", "currency": "INR", "price": nav,
            "market_time": self._stamp(day), "previous_close": prev, "market_state": "closed",
            "fifty_two_week_high": max(year), "fifty_two_week_low": min(year),
            "points": [{"t": self._stamp(d), "price": n} for d, n in series[-30:]]}}

    def fetch_profile(self, symbol: str) -> dict[str, Any]:
        scheme = self.data.scheme(mf_code(symbol))
        cat = scheme.category or ""
        asset, _, sub = cat.partition(" - ")
        return {"meta": self._meta(symbol, "profile", scheme.nav_date or dt.date.today()), "data": {
            "symbol": symbol, "name": scheme.name, "sector": f"MF · {asset.replace(' Scheme', '')}" if asset else "Mutual fund",
            "industry": sub or None, "quote_type": "MUTUALFUND", "exchange": scheme.amc}}

    def fetch_announcements(self, symbol: str, lookback_days: int) -> dict[str, Any]:
        raise NoDataAvailable("mutual funds have no announcements feed")


class CompositeProvider:
    """Routes ``MF<code>`` symbols to AMFI and everything else to the market-data provider."""

    def __init__(self, primary: Any, funds: AmfiProvider):
        self.primary, self.funds = primary, funds
        self.name = primary.name

    def _route(self, symbol: str) -> Any:
        return self.funds if is_mf_symbol(symbol) else self.primary

    def __getattr__(self, attr: str) -> Any:
        if not attr.startswith("fetch_"):
            return getattr(self.primary, attr)

        def call(symbol: str, *args: Any, **kwargs: Any) -> Any:
            target = getattr(self._route(symbol), attr, None)
            if target is None:
                raise NoDataAvailable(f"{attr} is not available for {symbol}")
            return target(symbol, *args, **kwargs)
        return call
