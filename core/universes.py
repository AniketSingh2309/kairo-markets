"""Stock universes (what the screener and Explore scan) and instrument search.

Every universe is built from an official list (see ``tools/providers/listings.py``); no tickers are
typed in by hand. Universes are capped at 500 names so a scan stays within the price source's rate limits.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable

from core import screener
from tools.providers.listings import Listing, ListingStore, ListingUnavailable

MAX_SCAN = 500


@dataclass(frozen=True)
class UniverseDef:
    key: str
    label: str
    group: str
    source: str                                    # shown to the user: where the list comes from
    build: Callable[[ListingStore], list[Listing]]


@dataclass
class Universe:
    key: str
    label: str
    group: str
    source: str
    members: list[Listing]

    @property
    def symbols(self) -> list[str]:
        return [m.symbol for m in self.members]

    def sector(self, symbol: str) -> str | None:
        return next((m.sector for m in self.members if m.symbol == symbol), None)


# ------------------------------------------------------------------------------------------ definitions

NIFTY_BROAD = [("nifty50", "Nifty 50"), ("niftynext50", "Nifty Next 50"), ("niftymidcap150", "Nifty Midcap 150"),
               ("niftysmallcap250", "Nifty Smallcap 250"), ("nifty500", "Nifty 500")]
NIFTY_SECTORS = [("niftybank", "Bank"), ("niftyit", "IT"), ("niftypharma", "Pharma"), ("niftyhealthcare", "Healthcare"),
                 ("niftyauto", "Auto"), ("niftyfmcg", "FMCG"), ("niftymetal", "Metal"), ("niftyrealty", "Realty"),
                 ("niftyenergy", "Energy"), ("niftyoilgas", "Oil & Gas"), ("niftyfinance", "Financial Services"),
                 ("niftypsubank", "PSU Bank"), ("niftymedia", "Media"), ("niftyconsumerdurables", "Consumer Durables"),
                 ("niftypse", "PSE"), ("niftyinfra", "Infrastructure")]
US_SECTORS = ["Technology", "Health Care", "Finance", "Consumer Discretionary", "Industrials", "Energy",
              "Consumer Staples", "Utilities", "Real Estate", "Telecommunications", "Basic Materials"]
NIFTY_SOURCE = "Official constituents, niftyindices.com"
NASDAQ_SOURCE = "All US-listed stocks by market cap, nasdaq.com"
BSE_SOURCE = "All active BSE companies by market cap, bseindia.com"


def _bse_stocks(store: ListingStore) -> list[Listing]:
    return [m for m in store.bse_equities() if m.kind == "stock"]


def _bse_only(store: ListingStore) -> list[Listing]:
    """BSE companies that have no NSE listing (matched by ISIN)."""
    on_nse = {m.isin for m in store.nse_equities() if m.isin}
    return [m for m in store.bse_equities() if m.kind == "stock" and m.isin not in on_nse]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _largest(rows: list[Listing], n: int) -> list[Listing]:
    """Top n by market cap, one line per company (GOOG/GOOGL, BRK-A/BRK-B count once)."""
    seen: set[str] = set()
    out = []
    for r in sorted((r for r in rows if r.market_cap), key=lambda r: -(r.market_cap or 0)):
        name = r.name.lower()
        if name in seen:
            continue
        seen.add(name)
        out.append(r)
        if len(out) == n:
            break
    return out


def _nasdaq100(store: ListingStore) -> list[Listing]:
    by_sym = {r.symbol: r for r in store.us_stocks()}
    return [by_sym.get(s) or Listing(s, s, "US", "stock") for s in store.nasdaq100()]


def default_defs() -> list[UniverseDef]:
    defs = [UniverseDef(slug, label, "India", NIFTY_SOURCE, lambda st, s=slug: st.nifty(s)) for slug, label in NIFTY_BROAD]
    defs += [UniverseDef(slug, f"Nifty {label}", "India sectors", NIFTY_SOURCE, lambda st, s=slug: st.nifty(s))
             for slug, label in NIFTY_SECTORS]
    defs += [
        UniverseDef("bse_top100", "BSE top 100", "BSE", BSE_SOURCE, lambda st: _largest(_bse_stocks(st), 100)),
        UniverseDef("bse_top500", "BSE top 500", "BSE", BSE_SOURCE, lambda st: _largest(_bse_stocks(st), 500)),
        UniverseDef("bse_only", "BSE-only top 100", "BSE", "Largest companies listed on BSE but not on NSE, bseindia.com",
                    lambda st: _largest(_bse_only(st), 100)),
    ]
    defs += [
        UniverseDef("us_top100", "US top 100", "US", NASDAQ_SOURCE, lambda st: _largest(st.us_stocks(), 100)),
        UniverseDef("us_top500", "US top 500", "US", NASDAQ_SOURCE, lambda st: _largest(st.us_stocks(), 500)),
        UniverseDef("nasdaq100", "Nasdaq-100", "US", "Official constituents, nasdaq.com", _nasdaq100),
    ]
    defs += [UniverseDef(f"us_{_slug(sec)}", f"US {sec}", "US sectors", f"Top 50 {sec} stocks by market cap, nasdaq.com",
                         lambda st, sec=sec: _largest([r for r in st.us_stocks() if r.sector == sec], 50))
             for sec in US_SECTORS]
    defs.append(UniverseDef("crypto", "Top crypto", "Crypto", "Largest coins by market cap (stablecoins excluded), Yahoo",
                            lambda st: st.crypto()[:30]))
    return defs


# ------------------------------------------------------------------------------------------ registry


@dataclass
class Hit:
    symbol: str
    name: str
    exchange: str
    kind: str
    sector: str | None


@dataclass
class Universes:
    store: ListingStore | None
    defs: list[UniverseDef] = field(default_factory=default_defs)
    mock_symbols: list[str] | None = None
    _counts: dict[str, int] = field(default_factory=dict, init=False)
    _index: tuple[float, list[tuple[Listing, int, str, str]]] | None = field(default=None, init=False)
    _index_lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    @classmethod
    def for_mock(cls, symbols: list[str]) -> Universes:
        good = [s for s in symbols if re.fullmatch(r"[A-Z]{1,5}", s)]
        mock = UniverseDef("mock", "Mock market (fictional)", "Mock", "tools/data/mock_market_data.json",
                           lambda _st: [Listing(s, s, "Mock", "stock") for s in good])
        return cls(store=None, defs=[mock], mock_symbols=good)

    @property
    def default_key(self) -> str:
        return self.defs[0].key

    def _def(self, key: str) -> UniverseDef | None:
        return next((d for d in self.defs if d.key == key), None)

    def known(self, key: str) -> bool:
        return key in screener.UNIVERSES or self._def(key) is not None

    def get(self, key: str) -> Universe:
        """Raises KeyError for an unknown key, ListingUnavailable when its list can't be downloaded."""
        if key in screener.UNIVERSES:  # lists registered in code (tests, custom deployments)
            label, syms = screener.UNIVERSES[key]
            return Universe(key, label, "Custom", "Registered in code", [Listing(s, s, "", "stock") for s in syms])
        d = self._def(key)
        if d is None:
            raise KeyError(key)
        members = d.build(self.store)[:MAX_SCAN]  # type: ignore[arg-type]
        if not members:
            raise ListingUnavailable(f"the {d.label} list came back empty")
        self._counts[key] = len(members)
        return Universe(d.key, d.label, d.group, d.source, members)

    def catalog(self) -> list[dict]:
        extra = [{"key": k, "label": label, "group": "Custom", "count": len(syms), "source": "Registered in code"}
                 for k, (label, syms) in screener.UNIVERSES.items()]
        return [{"key": d.key, "label": d.label, "group": d.group, "count": self._counts.get(d.key), "source": d.source}
                for d in self.defs] + extra

    def warm(self) -> None:
        """Download every list once (startup, background thread). Failures are left for request time."""
        for d in self.defs:
            try:
                self.get(d.key)
            except (ListingUnavailable, KeyError, ValueError):
                pass
        try:
            self._search_index()
        except ListingUnavailable:
            pass

    # --- search ---------------------------------------------------------------------------------

    def _search_index(self) -> list[tuple[Listing, int, str, str]]:
        """(listing, prominence tier, lowercase base symbol, lowercase name) for every instrument."""
        if self._index and time.time() - self._index[0] < 3600:
            return self._index[1]
        with self._index_lock:
            if self._index and time.time() - self._index[0] < 3600:
                return self._index[1]
            rows: list[tuple[Listing, int]] = []
            if self.store is None:
                rows = [(Listing(s, s, "Mock", "stock"), 0) for s in self.mock_symbols or []]
            else:
                tiers: dict[str, int] = {}
                sectors: dict[str, str] = {}
                for slug, tier in (("nifty500", 1), ("niftynext50", 2), ("nifty50", 3)):
                    try:
                        for m in self.store.nifty(slug):
                            tiers[m.symbol] = tier
                            if m.sector:
                                sectors[m.symbol] = m.sector
                    except (ListingUnavailable, ValueError):
                        pass
                loaded = 0
                try:
                    nse_isins = {m.isin for m in self.store.nse_equities() if m.isin}
                    bse_only = [m for m in self.store.bse_equities() if m.kind == "stock" and m.isin not in nse_isins]
                except (ListingUnavailable, ValueError):
                    bse_only = []
                for loader in (self.store.nse_equities, self.store.nse_etfs, lambda: bse_only, self.store.us_stocks,
                               self.store.crypto):
                    try:
                        items = loader()
                    except (ListingUnavailable, ValueError):
                        continue
                    loaded += 1
                    for m in items:
                        if m.exchange == "BSE":
                            cap = m.market_cap or 0  # rupees
                            tier = 2 if cap > 1e12 else 1 if cap > 5e10 else 0
                        elif m.exchange == "NSE":
                            tier = tiers.get(m.symbol, 0)
                            if m.symbol in sectors and not m.sector:
                                m = Listing(m.symbol, m.name, m.exchange, m.kind, sectors[m.symbol], m.industry, m.market_cap, m.isin)
                        else:
                            cap = m.market_cap or 0
                            tier = 3 if cap > 2e11 else 2 if cap > 2e10 else 1 if cap > 2e9 else 0
                        rows.append((m, tier))
                if not loaded:
                    raise ListingUnavailable("no stock list could be downloaded")
            index = [(m, tier, re.split(r"[.\-]", m.symbol)[0].lower() if m.exchange != "Crypto" else m.symbol.lower(),
                      m.name.lower()) for m, tier in rows]
            self._index = (time.time(), index)
            return index

    def search(self, query: str, limit: int = 10) -> list[Hit]:
        q = query.strip().lower()
        if not q:
            return []
        if q.isdigit() and len(q) == 6:  # a BSE scrip code, e.g. 500325
            found = self.by_bse_code(q)
            return [Hit(found.symbol, found.name, found.exchange, found.kind, found.sector)] if found else []
        words, squashed = q.split(), q.replace(" ", "")
        scored = []
        for m, tier, base, name in self._search_index():
            sym = m.symbol.lower()
            if base == q or sym == q or base == squashed:  # "nifty bees" -> NIFTYBEES
                rank = 0
            elif base.startswith(q) or sym.startswith(q) or name.startswith(q):
                rank = 1  # "info" finds Infosys (name) and InfoBeans (symbol) alike; size breaks the tie
            elif all(re.search(rf"\b{re.escape(w)}", name) for w in words):
                rank = 2
            elif q in name or q in sym:
                rank = 3
            else:
                continue
            scored.append(((rank, -tier, -(m.market_cap or 0), len(m.name)), m))
        scored.sort(key=lambda x: x[0])
        return [Hit(m.symbol, m.name, m.exchange, m.kind, m.sector) for _, m in scored[:limit]]

    # --- NSE <-> BSE ------------------------------------------------------------------------------

    def _venue_maps(self) -> tuple[dict[str, Listing], dict[str, Listing], dict[str, Listing], dict[str, str]]:
        """(NSE by ISIN, BSE by ISIN, BSE by scrip code, ISIN by symbol), rebuilt when the lists refresh."""
        if self.store is None:
            return {}, {}, {}, {}
        try:
            nse, bse = self.store.nse_equities(), self.store.bse_equities()
        except (ListingUnavailable, ValueError):
            return {}, {}, {}, {}
        cached = getattr(self, "_venues", None)
        if cached is None or cached[0] is not nse or cached[1] is not bse:
            maps = ({m.isin: m for m in nse if m.isin}, {m.isin: m for m in bse if m.isin},
                    {m.code: m for m in bse if m.code}, {m.symbol: m.isin for m in [*nse, *bse] if m.isin})
            cached = (nse, bse, maps)
            self._venues = cached
        return cached[2]

    def venues(self, symbol: str) -> dict[str, str | None]:
        """The same company on each Indian exchange: {"NSE": "M&M.NS", "BSE": "M&M.BO", "isin": ..., "bse_code": ...}."""
        nse_by_isin, bse_by_isin, _, isin_by_symbol = self._venue_maps()
        isin = isin_by_symbol.get(symbol)
        if not isin:
            return {"NSE": None, "BSE": None, "isin": None, "bse_code": None}
        n, b = nse_by_isin.get(isin), bse_by_isin.get(isin)
        return {"NSE": n.symbol if n else None, "BSE": b.symbol if b else None, "isin": isin, "bse_code": b.code if b else None}

    def by_bse_code(self, code: str) -> Listing | None:
        """A BSE scrip code -> the company, as its NSE line when it has one."""
        nse_by_isin, _, bse_by_code, _ = self._venue_maps()
        b = bse_by_code.get(code)
        if b is None:
            return None
        return nse_by_isin.get(b.isin or "") or b

    def listing(self, symbol: str) -> Listing | None:
        """The official-list entry for a symbol (name, ISIN, sector), if it's on one of the lists."""
        try:
            index = self._search_index()
        except ListingUnavailable:
            return None
        by_sym = getattr(self, "_by_symbol", None)
        if by_sym is None or by_sym[0] is not index:
            by_sym = (index, {m.symbol: m for m, *_ in index})
            self._by_symbol = by_sym
        return by_sym[1].get(symbol)


@lru_cache(maxsize=4)
def shared_universes(cache_dir: Path) -> Universes:
    """One registry (and one set of downloaded lists) per cache folder, shared by the API and the provider."""
    return Universes(ListingStore(cache_dir=cache_dir))
