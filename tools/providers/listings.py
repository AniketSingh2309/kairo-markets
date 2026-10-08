"""Official stock lists: which instruments exist, their names, and which index or sector they belong to.

This module answers "which stocks?", never "at what price?" (prices still come from the market
provider). Nothing here is typed in by hand: every list is downloaded from its publisher.

  NSE equities / ETFs    nsearchives.nseindia.com   (every listed company and ETF)
  BSE equities           api.bseindia.com           (every active BSE company, with its scrip code and market cap)
  Nifty index members    niftyindices.com           (official constituent files)
  US stocks              api.nasdaq.com             (every NASDAQ / NYSE / AMEX stock, with market cap and sector)
  Nasdaq-100             api.nasdaq.com
  Crypto                 Yahoo's "all cryptocurrencies" screener, by market cap

Each download is cached on disk and refreshed weekly. If a publisher is unreachable, the last good
copy is used (and reported as stale), so one site being down never empties the app.
"""

from __future__ import annotations

import csv
import io
import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from tools.models import SYMBOL_PATTERN

BROWSER_HEADERS = {
    # NSE, niftyindices and Nasdaq reject requests that don't look like a browser.
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/126.0 Safari/537.36",
    "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9",
}
NIFTY_URL = "https://www.niftyindices.com/IndexConstituent/ind_{slug}list.csv"
SOURCES: dict[str, str] = {
    "nse_equity": "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
    "nse_etf": "https://nsearchives.nseindia.com/content/equities/eq_etfseclist.csv",
    "bse_equity": "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w?Group=&Scripcode=&industry=&segment=Equity&status=Active",
    "us_stocks": "https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true",
    "nasdaq100": "https://api.nasdaq.com/api/quote/list-type/nasdaq100",
    "crypto": "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=all_cryptocurrencies_us&count=100",
}
MAX_AGE_S = 7 * 24 * 3600
# BSE's API answers "Access Denied" unless the request looks like it comes from bseindia.com.
BSE_HEADERS = {"Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9",
               "Origin": "https://www.bseindia.com", "Referer": "https://www.bseindia.com/"}


class ListingUnavailable(Exception):
    """A list couldn't be downloaded and there is no earlier copy on disk."""


@dataclass(frozen=True)
class Listing:
    symbol: str                    # as the market provider (Yahoo) spells it: INFY.NS, BRK-B, BTC-USD
    name: str
    exchange: str                  # NSE, US, Crypto
    kind: str                      # stock, etf, crypto
    sector: str | None = None
    industry: str | None = None
    market_cap: float | None = None
    isin: str | None = None
    code: str | None = None        # the exchange's own number, e.g. BSE scrip code 500325


@dataclass
class Fetched:
    text: str
    fetched_at: float              # epoch seconds
    stale: bool = False            # True when the refresh failed and an older copy is being served


# ------------------------------------------------------------------------------------------ parsers


def _valid(symbol: str) -> bool:  # also drops NSE placeholder rows (DUMMYHEG, DUMMYSAN, ...)
    return bool(SYMBOL_PATTERN.fullmatch(symbol)) and not symbol.startswith("DUMMY")


def _name(raw: str, fallback: str) -> str:
    """NSE writes some names in capitals ("ETERNAL LIMITED"); show them like the rest."""
    name = raw.strip() or fallback
    return name.title() if name.isupper() and len(name) > 6 else name


def _rows(text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    return [{(k or "").strip().upper(): (v or "").strip() for k, v in row.items()} for row in reader]


def parse_nse_equity(text: str) -> list[Listing]:
    out = []
    for r in _rows(text):
        sym = r.get("SYMBOL", "")
        if sym and _valid(f"{sym}.NS"):
            out.append(Listing(f"{sym}.NS", _name(r.get("NAME OF COMPANY", ""), sym), "NSE", "stock", isin=r.get("ISIN NUMBER") or None))
    return out


def parse_nse_etf(text: str) -> list[Listing]:
    out = []
    for r in _rows(text):
        sym = r.get("SYMBOL", "")
        under = r.get("UNDERLYING ASSET") or r.get("UNDERLYING KEY") or sym
        if sym and _valid(f"{sym}.NS"):
            name = under if "etf" in under.lower() else f"{under} ETF"
            out.append(Listing(f"{sym}.NS", name, "NSE", "etf", sector=r.get("ETF UNDERLYING") or None,
                               isin=r.get("ISINNUMBER") or None))
    return out


def parse_nifty_index(text: str) -> list[Listing]:
    if not text.lstrip("﻿").startswith("Company Name"):
        raise ValueError("not an index constituent file")
    out = []
    for r in _rows(text):
        sym = r.get("SYMBOL", "")
        if sym and _valid(f"{sym}.NS"):
            out.append(Listing(f"{sym}.NS", _name(r.get("COMPANY NAME", ""), sym), "NSE", "stock",
                               sector=r.get("INDUSTRY") or None, isin=r.get("ISIN CODE") or None))
    return out


def parse_bse_equity(text: str) -> list[Listing]:
    """BSE's active equity list. Symbols use the BSE scrip id with Yahoo's .BO suffix (RELIANCE.BO)."""
    rows = json.loads(text)
    if not isinstance(rows, list):
        raise ValueError("BSE list is not a JSON array")
    out = []
    for r in rows:
        if not isinstance(r, dict) or (r.get("Status") or "Active") != "Active":
            continue
        sid, code = (r.get("scrip_id") or "").strip().upper(), str(r.get("SCRIP_CD") or "").strip()
        sym = f"{sid}.BO"
        if not sid or not _valid(sym):
            continue
        cap = _num(r.get("Mktcap"))  # rupees crore
        isin = (r.get("ISIN_NUMBER") or "").strip()
        kind = "etf" if isin.startswith("INF") else "stock"  # BSE's equity segment also lists ETFs / fund units
        out.append(Listing(sym, _name(r.get("Issuer_Name") or r.get("Scrip_Name") or "", sid), "BSE", kind,
                           industry=r.get("INDUSTRY") or None, market_cap=cap * 1e7 if cap else None,
                           isin=(r.get("ISIN_NUMBER") or "").strip() or None, code=code or None))
    return out


_US_NAME_TAIL = re.compile(r"\s+(?:Class [A-Z] )?(?:Common Stock|Ordinary Shares|Common Shares|Capital Stock|"
                           r"American Depositary Shares?|Depositary Shares)\b.*$", re.I)


def _us_symbol(raw: str) -> str | None:
    """Nasdaq writes share classes as BRK/B; Yahoo as BRK-B. Preferreds (ABR^D) and units are skipped."""
    if "^" in raw or "$" in raw or " " in raw:
        return None
    sym = raw.replace("/", "-").replace(".", "-").upper()
    return sym if _valid(sym) else None


def _num(value: Any) -> float | None:
    try:
        v = float(str(value).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def parse_us_stocks(text: str) -> list[Listing]:
    rows = json.loads(text)["data"]["rows"] or []
    out = []
    for r in rows:
        sym = _us_symbol(r.get("symbol") or "")
        if not sym:
            continue
        name = _US_NAME_TAIL.sub("", (r.get("name") or sym).strip()) or sym
        out.append(Listing(sym, name, "US", "stock", sector=(r.get("sector") or None), industry=(r.get("industry") or None),
                           market_cap=_num(r.get("marketCap"))))
    return out


def parse_nasdaq100(text: str) -> list[str]:
    rows = json.loads(text)["data"]["data"]["rows"] or []
    return [s for r in rows if (s := _us_symbol(r.get("symbol") or ""))]


_NOT_A_COIN = re.compile(r"wrapped|staked|bridged|restaked|liquid staking|tether|usd coin|\bdai\b|usds|usde|"
                         r"first digital|paypal usd|binance-peg|\bw?steth\b|\bweth\b|\bbep2\b|\btrc20\b|\bbtcb\b", re.I)


def parse_crypto(text: str) -> list[Listing]:
    """Top coins by market cap, without stablecoins and wrapped / staked copies of other coins."""
    quotes = json.loads(text)["finance"]["result"][0]["quotes"]
    out = []
    for q in quotes:
        sym, name = q.get("symbol") or "", (q.get("shortName") or q.get("longName") or "").strip()
        price = _num(q.get("regularMarketPrice"))
        if not _valid(sym) or _NOT_A_COIN.search(f"{name} {sym}"):
            continue
        if sym.startswith("USD") or (price and 0.97 < price < 1.03 and "usd" in name.lower().replace(sym.lower(), "")):
            continue  # stablecoins: nothing to screen
        clean = re.sub(r"\s+USD$", "", name) or sym
        out.append(Listing(sym, clean, "Crypto", "crypto", market_cap=_num(q.get("marketCap"))))
    return out


# ------------------------------------------------------------------------------------------ store


@dataclass
class ListingStore:
    """Downloads, caches (memory + disk) and parses the lists. Thread-safe."""

    cache_dir: Path | None = None
    client: httpx.Client | None = None
    max_age_s: float = MAX_AGE_S
    _mem: dict[str, Fetched] = field(default_factory=dict, init=False)
    _parsed: dict[tuple[str, float], Any] = field(default_factory=dict, init=False)
    _locks: dict[str, threading.Lock] = field(default_factory=dict, init=False)
    _guard: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = httpx.Client(headers=BROWSER_HEADERS, timeout=30, follow_redirects=True)

    # --- raw text -------------------------------------------------------------------------------

    @staticmethod
    def url(name: str) -> str:
        if name.startswith("nifty:"):
            return NIFTY_URL.format(slug=name.split(":", 1)[1])
        return SOURCES[name]

    def _path(self, name: str) -> Path | None:
        return self.cache_dir / f"{name.replace(':', '_')}.txt" if self.cache_dir else None

    def _lock(self, name: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(name, threading.Lock())

    def peek(self, name: str) -> Fetched | None:
        """What's in memory or on disk, without touching the network."""
        if name in self._mem:
            return self._mem[name]
        path = self._path(name)
        if path and path.exists():
            got = Fetched(path.read_text(encoding="utf-8"), path.stat().st_mtime)
            self._mem[name] = got
            return got
        return None

    def raw(self, name: str, validate: Callable[[str], Any] | None = None) -> Fetched:
        have = self.peek(name)
        if have and time.time() - have.fetched_at < self.max_age_s:
            return have
        with self._lock(name):
            have = self.peek(name)
            if have and time.time() - have.fetched_at < self.max_age_s:
                return have
            try:
                url = self.url(name)
                extra = BSE_HEADERS if "bseindia" in url else {"Accept": "application/json"} if "nasdaq" in url else None
                resp = self.client.get(url, headers=extra)
                resp.raise_for_status()
                text = resp.text
                if validate:
                    validate(text)  # don't overwrite a good copy with an error page
            except Exception as exc:  # noqa: BLE001 - any failure falls back to the last good copy
                if have:
                    have.stale = True
                    return have
                raise ListingUnavailable(f"couldn't download the {name} list: {type(exc).__name__}") from None
            got = Fetched(text, time.time())
            self._mem[name] = got
            if (path := self._path(name)) is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            return got

    def _parse(self, name: str, parser: Callable[[str], Any]) -> Any:
        got = self.raw(name, parser)
        key = (name, got.fetched_at)
        if key not in self._parsed:
            self._parsed[key] = parser(got.text)
        return self._parsed[key]

    # --- lists ------------------------------------------------------------------------------------

    def nse_equities(self) -> list[Listing]:
        return self._parse("nse_equity", parse_nse_equity)

    def nse_etfs(self) -> list[Listing]:
        return self._parse("nse_etf", parse_nse_etf)

    def bse_equities(self) -> list[Listing]:
        return self._parse("bse_equity", parse_bse_equity)

    def nifty(self, slug: str) -> list[Listing]:
        return self._parse(f"nifty:{slug}", parse_nifty_index)

    def us_stocks(self) -> list[Listing]:
        return self._parse("us_stocks", parse_us_stocks)

    def nasdaq100(self) -> list[str]:
        return self._parse("nasdaq100", parse_nasdaq100)

    def crypto(self) -> list[Listing]:
        return self._parse("crypto", parse_crypto)

    def status(self) -> dict[str, dict[str, Any]]:
        out = {}
        for name, got in list(self._mem.items()):
            out[name] = {"url": self.url(name), "fetched_at": got.fetched_at, "stale": got.stale}
        return out
