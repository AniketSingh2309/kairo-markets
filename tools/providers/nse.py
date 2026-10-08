"""NSE derivatives data: F&O underlyings, expiries, option chains and option-contract price series.

nseindia.com serves JSON only to a browser-like session: the first request loads the option-chain
page to collect cookies, and an expired session (401/403 or a non-JSON reply) is renewed once.
Everything is cached briefly; NSE's own data refreshes every few seconds during market hours.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from tools.providers.base import NoDataAvailable, ProviderUnavailable, SymbolNotFound
from tools.providers.listings import BROWSER_HEADERS

BASE = "https://www.nseindia.com"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SYMBOL_RE = re.compile(r"^[A-Z0-9&\-]{1,20}$")
EXPIRY_RE = re.compile(r"^\d{2}-[A-Z][a-z]{2}-\d{4}$")
CONTRACT_RE = re.compile(r"^OPT(IDX|STK)([A-Z0-9&\-]+?)(\d{2}-\d{2}-\d{4})(CE|PE)(\d+(?:\.\d+)?)$")
# Yahoo index tickers <-> NSE derivative underlyings
YAHOO_TO_NSE = {"^NSEI": "NIFTY", "^NSEBANK": "BANKNIFTY", "NIFTY_FIN_SERVICE.NS": "FINNIFTY",
                "NIFTY_MID_SELECT.NS": "MIDCPNIFTY", "^NSMIDCP": "NIFTYNXT50"}
NSE_TO_YAHOO = {v: k for k, v in YAHOO_TO_NSE.items()}


def contract_label(identifier: str) -> str | None:
    """OPTIDXNIFTY13-10-2026CE22750.00 -> "NIFTY 13 Oct 22750 CE"."""
    m = CONTRACT_RE.match(identifier)
    if not m:
        return None
    _, sym, date, kind, strike = m.groups()
    day = dt.datetime.strptime(date, "%d-%m-%Y")
    return f"{sym} {day.day} {day:%b} {float(strike):g} {kind}"


def _side(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    num = lambda k: raw.get(k) if isinstance(raw.get(k), (int, float)) else None  # noqa: E731
    return {
        "id": raw.get("identifier"), "ltp": num("lastPrice"), "chg": num("change"), "pchg": num("pChange"),
        "oi": num("openInterest"), "coi": num("changeinOpenInterest"), "pcoi": num("pchangeinOpenInterest"),
        "vol": num("totalTradedVolume"), "iv": num("impliedVolatility"),
        "bid": num("buyPrice1"), "bid_qty": num("buyQuantity1"), "ask": num("sellPrice1"), "ask_qty": num("sellQuantity1"),
    }


def parse_chain(payload: Any, symbol: str, expiry: str) -> dict[str, Any]:
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, dict) or not isinstance(records.get("data"), list):
        raise ProviderUnavailable("NSE option chain came back empty (session or symbol issue)")
    spot = records.get("underlyingValue")
    rows = []
    for r in records["data"]:
        if not isinstance(r, dict) or r.get("strikePrice") is None:
            continue
        if r.get("expiryDates") and r["expiryDates"] != expiry:
            continue
        rows.append({"strike": float(r["strikePrice"]), "ce": _side(r.get("CE")), "pe": _side(r.get("PE"))})
    rows.sort(key=lambda r: r["strike"])
    if not rows:
        raise NoDataAvailable(f"no {symbol} options for expiry {expiry}")
    ce_oi = sum((r["ce"] or {}).get("oi") or 0 for r in rows)
    pe_oi = sum((r["pe"] or {}).get("oi") or 0 for r in rows)
    atm = min(rows, key=lambda r: abs(r["strike"] - spot))["strike"] if isinstance(spot, (int, float)) else None
    stamp = records.get("timestamp")
    try:
        observed = dt.datetime.strptime(stamp, "%d-%b-%Y %H:%M:%S").replace(tzinfo=IST).isoformat() if stamp else None
    except ValueError:
        observed = None
    return {"symbol": symbol, "expiry": expiry, "expiries": records.get("expiryDates") or [], "spot": spot,
            "observed_at": observed, "atm": atm, "rows": rows,
            "totals": {"ce_oi": ce_oi, "pe_oi": pe_oi, "pcr": round(pe_oi / ce_oi, 3) if ce_oi else None,
                       "max_pain": max_pain(rows)}}


def max_pain(rows: list[dict[str, Any]]) -> float | None:
    """Strike at which option writers' total payout to buyers would be smallest at expiry."""
    strikes = [r["strike"] for r in rows]
    if not strikes:
        return None
    def payout(expiry_price: float) -> float:
        total = 0.0
        for r in rows:
            k = r["strike"]
            total += max(0.0, expiry_price - k) * ((r["ce"] or {}).get("oi") or 0)
            total += max(0.0, k - expiry_price) * ((r["pe"] or {}).get("oi") or 0)
        return total
    if not any((r["ce"] or {}).get("oi") or (r["pe"] or {}).get("oi") for r in rows):
        return None
    return min(strikes, key=payout)


# Shareholding-pattern XBRL contexts -> the groups apps show (as % of all shares)
SHP_GROUPS = {
    "promoters": "ShareholdingOfPromoterAndPromoterGroup_ContextI",
    "fii": "InstitutionsForeign_ContextI",
    "mutual_funds": "MutualFundsOrUTI_ContextI",
    "dii": "InstitutionsDomestic_ContextI",          # all domestic institutions, mutual funds included
    "government": "Governments_ContextI",
}
_SHP_VALUE = re.compile(r'<[^>]*:ShareholdingAsAPercentageOfTotalNumberOfShares[^>]*contextRef="([^"]+)"[^>]*>([^<]+)<')


def parse_shareholding_xbrl(text: str) -> dict[str, float]:
    """Percentages (0-100) by holder group from an NSE shareholding-pattern XBRL filing."""
    values: dict[str, float] = {}
    for ctx, raw in _SHP_VALUE.findall(text):
        try:
            values.setdefault(ctx, float(raw))
        except ValueError:
            continue
    out = {k: round(values[ctx] * 100, 2) for k, ctx in SHP_GROUPS.items() if ctx in values}
    if not out:
        raise ValueError("no holding percentages in this filing")
    out.setdefault("promoters", 0.0)  # widely held companies (e.g. HDFC Bank) have no promoter group
    if "dii" in out and "mutual_funds" in out:
        out["other_dii"] = round(max(0.0, out["dii"] - out["mutual_funds"]), 2)
    taken = sum(out.get(k, 0.0) for k in ("promoters", "fii", "dii", "government"))
    out["retail_and_others"] = round(max(0.0, 100 - taken), 2)
    return out


@dataclass
class NseClient:
    client: httpx.Client | None = None
    cache_dir: Path | None = None
    _warm_at: float = field(default=0.0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _cache: dict[Any, tuple[float, Any]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = httpx.Client(timeout=10, follow_redirects=True, headers={
                **BROWSER_HEADERS, "Accept": "application/json, text/plain, */*", "Referer": f"{BASE}/option-chain"})

    def _session(self, force: bool = False) -> None:
        with self._lock:
            if force or time.time() - self._warm_at > 20 * 60:
                try:
                    self.client.get(f"{BASE}/option-chain", headers={"Accept": "text/html"})
                except httpx.HTTPError as exc:
                    raise ProviderUnavailable(f"NSE unreachable: {type(exc).__name__}") from exc
                self._warm_at = time.time()

    def _json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        for attempt in range(2):
            self._session(force=attempt > 0)
            try:
                r = self.client.get(f"{BASE}{path}", params=params)
            except httpx.HTTPError as exc:
                raise ProviderUnavailable(f"NSE request failed: {type(exc).__name__}") from exc
            if r.status_code == 404:
                raise SymbolNotFound(f"NSE has no data at {path}")
            if r.status_code in (401, 403) or "json" not in r.headers.get("content-type", ""):
                continue  # session expired: renew cookies once
            if r.status_code != 200:
                raise ProviderUnavailable(f"NSE returned HTTP {r.status_code}")
            return r.json()
        raise ProviderUnavailable("NSE refused the request (session not accepted)")

    def _cached(self, key: Any, ttl: float, load) -> Any:
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        value = load()
        if len(self._cache) > 500:
            self._cache.clear()
        self._cache[key] = (time.monotonic(), value)
        return value

    # --- public -------------------------------------------------------------------------------------

    def underlyings(self) -> dict[str, list[dict[str, str]]]:
        def load():
            data = (self._json("/api/underlying-information") or {}).get("data") or {}
            idx = [{"symbol": x["symbol"], "name": x.get("underlying") or x["symbol"], "type": "index"}
                   for x in data.get("IndexList") or [] if SYMBOL_RE.match(x.get("symbol", ""))]
            stk = [{"symbol": x["symbol"], "name": x.get("underlying") or x["symbol"], "type": "stock"}
                   for x in data.get("UnderlyingList") or [] if SYMBOL_RE.match(x.get("symbol", ""))]
            if not idx and not stk:
                raise ProviderUnavailable("NSE returned no F&O underlyings")
            return {"indices": idx, "stocks": stk}
        return self._cached("underlyings", 6 * 3600, load)

    def is_index(self, symbol: str) -> bool:
        return any(x["symbol"] == symbol for x in self.underlyings()["indices"])

    def expiries(self, symbol: str) -> list[str]:
        def load():
            dates = (self._json("/api/option-chain-contract-info", {"symbol": symbol}) or {}).get("expiryDates") or []
            if not dates:
                raise SymbolNotFound(f"{symbol} has no option expiries on NSE")
            return dates
        return self._cached(("expiries", symbol), 3600, load)

    def chain(self, symbol: str, expiry: str | None, ttl: float = 5) -> dict[str, Any]:
        expiries = self.expiries(symbol)
        expiry = expiry if expiry in expiries else expiries[0]
        kind = "Indices" if self.is_index(symbol) else "Equity"
        def load():
            payload = self._json("/api/option-chain-v3", {"type": kind, "symbol": symbol, "expiry": expiry})
            return {**parse_chain(payload, symbol, expiry), "expiries": expiries}
        return self._cached(("chain", symbol, expiry), ttl, load)

    def lot_sizes(self) -> dict[str, dict[str, int]]:
        """{symbol: {"OCT-26": 65, ...}} from NSE's F&O market-lots file (lot sizes change by expiry month)."""
        def load():
            self._session()
            r = self.client.get("https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv", headers={"Accept": "text/csv,*/*"})
            if r.status_code != 200:
                raise ProviderUnavailable(f"NSE lot-size file returned HTTP {r.status_code}")
            return parse_lot_sizes(r.text)
        return self._cached("lots", 24 * 3600, load)

    def lot_size(self, symbol: str, expiry: str) -> int | None:
        try:
            month = dt.datetime.strptime(expiry, "%d-%b-%Y").strftime("%b-%y").upper()
            return self.lot_sizes().get(symbol, {}).get(month)
        except (ValueError, ProviderUnavailable, httpx.HTTPError):
            return None

    def shareholding(self, symbol: str, quarters: int = 4) -> dict[str, Any]:
        """Promoter / FII / mutual fund / other DII / retail holdings for the last few quarters."""
        if not SYMBOL_RE.match(symbol):
            raise SymbolNotFound(f"not an NSE symbol: {symbol!r}")
        def load():
            rows = self._json("/api/corporate-share-holdings-master", {"index": "equities", "symbol": symbol}) or []
            if not isinstance(rows, list) or not rows:
                raise SymbolNotFound(f"NSE has no shareholding pattern for {symbol}")
            by_date: dict[str, dict] = {}
            for r in rows:
                if isinstance(r, dict) and r.get("date") and r["date"] not in by_date:
                    by_date[r["date"]] = r
            dated = sorted(by_date.values(), key=lambda r: dt.datetime.strptime(r["date"], "%d-%b-%Y"), reverse=True)
            out = []
            for r in dated[:quarters]:
                when = dt.datetime.strptime(r["date"], "%d-%b-%Y").date()
                q = {"quarter_end": when.isoformat(), "promoters": _pct(r.get("pr_and_prgrp")), "public": _pct(r.get("public_val"))}
                if r.get("xbrl"):
                    try:
                        q.update(self._shp_breakdown(r["xbrl"]))
                    except (ProviderUnavailable, ValueError, httpx.HTTPError):
                        pass  # the promoter / public split above still stands
                out.append(q)
            return {"symbol": symbol, "quarters": out}
        return self._cached(("shareholding", symbol), 12 * 3600, load)

    def _shp_breakdown(self, url: str) -> dict[str, float]:
        if not url.startswith("https://nsearchives.nseindia.com/corporate/xbrl/"):
            raise ValueError("unexpected filing location")
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", url.rsplit("/", 1)[-1]) + ".json"
        path = self.cache_dir / "shp" / name if self.cache_dir else None
        if path and path.exists():
            return json.loads(path.read_text(encoding="utf-8"))  # a filing never changes
        self._session()
        r = self.client.get(url, headers={"Accept": "*/*"})
        if r.status_code != 200:
            raise ProviderUnavailable(f"NSE filing returned HTTP {r.status_code}")
        data = parse_shareholding_xbrl(r.text)
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part"); tmp.write_text(json.dumps(data), encoding="utf-8"); os.replace(tmp, path)
        return data

    def contract(self, identifier: str, ttl: float = 5) -> dict[str, Any]:
        """Today's trades for one option contract: [(epoch seconds UTC, price)] plus the previous close."""
        if not CONTRACT_RE.match(identifier):
            raise SymbolNotFound(f"not an NSE option contract: {identifier!r}")
        def load():
            d = self._json("/api/chart-databyindex", {"index": identifier}) or {}
            pts = d.get("grapthData") or d.get("graphData") or []
            # NSE writes Indian wall-clock time as if it were UTC; shift back to real UTC.
            ticks = [(int(p[0] / 1000) - 19800, float(p[1])) for p in pts
                     if isinstance(p, list) and len(p) >= 2 and isinstance(p[1], (int, float)) and p[1] > 0]
            if not ticks:
                raise NoDataAvailable(f"no trades today for {identifier}")
            return {"id": identifier, "label": contract_label(identifier), "prev_close": d.get("closePrice"), "ticks": ticks}
        return self._cached(("contract", identifier), ttl, load)


def _pct(v: Any) -> float | None:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def parse_lot_sizes(text: str) -> dict[str, dict[str, int]]:
    rows = [[c.strip() for c in line.split(",")] for line in text.splitlines() if line.strip()]
    if not rows or len(rows[0]) < 3:
        return {}
    months = rows[0][2:]
    out: dict[str, dict[str, int]] = {}
    for row in rows[1:]:
        if len(row) < 3 or not row[1] or row[1].upper() == "SYMBOL":
            continue
        lots = {m: int(v) for m, v in zip(months, row[2:]) if m and v.isdigit()}
        if lots:
            out[row[1].upper()] = lots
    return out
