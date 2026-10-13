"""Market-wide Indian data from NSE that traders check every day.

* FII / DII cash-market flows (provisional, latest session)
* bulk, block and short deals
* corporate actions (dividends, splits, bonus, buybacks) and the board-meeting / results calendar
* surveillance lists: ASM (long and short term) and GSM, plus the F&O ban list
* delivery % for every stock, from the daily full bhavcopy
* IPOs: open, upcoming and recently listed
* participant-wise F&O open interest (FII / DII / pro / client positioning)

Everything is public NSE data. JSON endpoints go through ``NseClient``'s browser-like session; the
daily CSV files are cached on disk because a published file never changes. Parsers are pure functions.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import os
import re
from pathlib import Path
from typing import Any

import httpx

from tools.providers.base import NoDataAvailable, ProviderUnavailable
from tools.providers.nse import NseClient

ARCHIVES = "https://nsearchives.nseindia.com"


def _num(v: Any) -> float | None:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _date(v: Any, fmt: str = "%d-%b-%Y") -> str | None:
    try:
        return dt.datetime.strptime(str(v).strip()[:11], fmt).date().isoformat()
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------------------------------ parsers


def parse_fii_dii(rows: Any) -> dict[str, Any]:
    """[{category: "FII/FPI"|"DII", buyValue, sellValue, netValue, date}] -> one session, in ₹ crore."""
    if not isinstance(rows, list) or not rows:
        raise NoDataAvailable("NSE returned no FII/DII figures")
    out: dict[str, Any] = {"date": None}
    for r in rows:
        if not isinstance(r, dict):
            continue
        key = "fii" if "FII" in str(r.get("category", "")).upper() else "dii" if "DII" in str(r.get("category", "")).upper() else None
        if key:
            out[key] = {"buy": _num(r.get("buyValue")), "sell": _num(r.get("sellValue")), "net": _num(r.get("netValue"))}
            out["date"] = out["date"] or _date(r.get("date"))
    if "fii" not in out and "dii" not in out:
        raise NoDataAvailable("NSE's FII/DII reply had no FII or DII row")
    return out


def _deal(r: dict[str, Any]) -> dict[str, Any]:
    return {"symbol": str(r.get("symbol") or "").strip(), "name": r.get("name"), "client": r.get("clientName"),
            "side": str(r.get("buySell") or "").upper() or None, "qty": int(_num(r.get("qty")) or 0),
            "price": _num(r.get("watp")), "date": _date(r.get("date"))}


def parse_large_deals(d: Any) -> dict[str, Any]:
    if not isinstance(d, dict):
        raise NoDataAvailable("NSE returned no deals")
    pick = lambda k: [_deal(r) for r in d.get(k) or [] if isinstance(r, dict) and r.get("symbol")]  # noqa: E731
    out = {"as_on": _date(d.get("as_on_date")), "bulk": pick("BULK_DEALS_DATA"), "block": pick("BLOCK_DEALS_DATA"), "short": pick("SHORT_DEALS_DATA")}
    for k in ("bulk", "block", "short"):
        for x in out[k]:
            x["value_cr"] = round(x["qty"] * x["price"] / 1e7, 2) if x["price"] else None
    return out


EQUITY_SERIES = {"EQ", "BE", "BZ", "SM", "ST"}
_AMOUNT = re.compile(r"(?:Rs\.?|Re\.?|₹)\s*([\d.]+)", re.I)


def action_kind(subject: str) -> str:
    s = subject.lower()
    for kind, words in (("dividend", ("dividend",)), ("bonus", ("bonus",)), ("split", ("split", "sub-division", "subdivision")),
                        ("buyback", ("buy back", "buyback")), ("rights", ("rights",)), ("demerger", ("demerger",)), ("merger", ("amalgamation", "merger"))):
        if any(w in s for w in words):
            return kind
    return "other"


def parse_corporate_actions(rows: Any) -> list[dict[str, Any]]:
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or not r.get("symbol") or str(r.get("series") or "EQ").upper() not in EQUITY_SERIES:
            continue  # bonds and government securities (interest payments, redemptions) aren't stocks
        subject = str(r.get("subject") or "").strip()
        kind = action_kind(subject)
        m = _AMOUNT.search(subject) if kind == "dividend" else None
        out.append({"symbol": r["symbol"], "company": r.get("comp"), "subject": subject, "kind": kind,
                    "amount": float(m.group(1).rstrip(".")) if m else None,
                    "ex_date": _date(r.get("exDate")), "record_date": _date(r.get("recDate"))})
    return sorted(out, key=lambda x: (x["ex_date"] or "9999", x["symbol"]))


def parse_events(rows: Any) -> list[dict[str, Any]]:
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or not r.get("symbol"):
            continue
        purpose = str(r.get("purpose") or "").strip()
        out.append({"symbol": r["symbol"], "company": r.get("company"), "purpose": purpose, "detail": (r.get("bm_desc") or "").strip(),
                    "date": _date(r.get("date")), "results": "result" in purpose.lower()})
    return sorted(out, key=lambda x: (x["date"] or "9999", x["symbol"]))


def parse_surveillance(asm: Any, gsm: Any) -> dict[str, dict[str, str]]:
    """{symbol: {"asm": "Long-term ASM Stage I", "gsm": "GSM Stage 0", ...}}"""
    out: dict[str, dict[str, str]] = {}
    if isinstance(asm, dict):
        for term, label in (("longterm", "Long-term ASM"), ("shortterm", "Short-term ASM")):
            for r in (asm.get(term) or {}).get("data") or []:
                if isinstance(r, dict) and r.get("symbol"):
                    out.setdefault(r["symbol"], {})["asm"] = f"{label} {r.get('asmSurvIndicator') or ''}".strip()
    for r in gsm if isinstance(gsm, list) else []:
        if isinstance(r, dict) and r.get("symbol"):
            out.setdefault(r["symbol"], {})["gsm"] = r.get("survDesc") or f"GSM stage {r.get('gsmStage')}"
    return out


def parse_ban(text: str) -> dict[str, Any]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    day = None
    if lines and (m := re.search(r"(\d{2}-[A-Z]{3}-\d{4})", lines[0].upper())):
        day = _date(m.group(1).title())
    syms = [ln.split(",", 1)[1].strip().upper() for ln in lines[1:] if "," in ln and ln.split(",", 1)[0].strip().isdigit()]
    return {"date": day, "symbols": syms}


def parse_bhav_full(text: str) -> dict[str, dict[str, Any]]:
    """NSE's sec_bhavdata_full CSV -> {symbol: {close, prev_close, volume, turnover_cr, delivery_qty, delivery_pct}} (EQ/BE series)."""
    reader = csv.reader(io.StringIO(text))
    head = [h.strip().upper() for h in next(reader, [])]
    if "SYMBOL" not in head or "DELIV_PER" not in head:
        raise ProviderUnavailable("unexpected bhavcopy layout")
    ix = {h: i for i, h in enumerate(head)}
    out = {}
    for row in reader:
        if len(row) < len(head) or row[ix["SERIES"]].strip() not in ("EQ", "BE", "BZ", "SM", "ST"):
            continue
        sym = row[ix["SYMBOL"]].strip()
        out[sym] = {"close": _num(row[ix["CLOSE_PRICE"]]), "prev_close": _num(row[ix["PREV_CLOSE"]]),
                    "volume": int(_num(row[ix["TTL_TRD_QNTY"]]) or 0), "turnover_cr": round((_num(row[ix["TURNOVER_LACS"]]) or 0) / 100, 2),
                    "delivery_qty": int(_num(row[ix["DELIV_QTY"]]) or 0), "delivery_pct": _num(row[ix["DELIV_PER"]])}
    return out


def _ipo(r: dict[str, Any]) -> dict[str, Any]:
    return {"symbol": r.get("symbol"), "company": r.get("companyName") or r.get("company"), "series": r.get("series") or r.get("securityType"),
            "price": r.get("issuePrice") or r.get("priceRange"), "size": r.get("issueSize"), "status": r.get("status"),
            "start": _date(r.get("issueStartDate") or r.get("ipoStartDate")), "end": _date(r.get("issueEndDate") or r.get("ipoEndDate")),
            "subscribed_x": _num(r.get("noOfTime")), "listing_date": _date(r.get("listingDate"))}


def parse_ipos(current: Any, upcoming: Any, past: Any, today: dt.date, recent_days: int = 60) -> dict[str, Any]:
    cur = [_ipo(r) for r in current if isinstance(r, dict)] if isinstance(current, list) else []
    seen = {x["symbol"] for x in cur}
    up = [_ipo(r) for r in upcoming if isinstance(r, dict)] if isinstance(upcoming, list) else []
    up = [x for x in up if x["symbol"] not in seen]
    cutoff = (today - dt.timedelta(days=recent_days)).isoformat()
    listed = [_ipo(r) for r in past if isinstance(r, dict)] if isinstance(past, list) else []
    listed = sorted([x for x in listed if x["listing_date"] and x["listing_date"] >= cutoff], key=lambda x: x["listing_date"], reverse=True)
    return {"open": cur, "upcoming": up, "listed": listed[:30]}


PARTICIPANTS = ("Client", "DII", "FII", "Pro")


def parse_participant_oi(text: str) -> dict[str, Any]:
    """NSE's fao_participant_oi CSV -> per participant: index futures / options long and short, net."""
    rows = list(csv.reader(io.StringIO(text)))
    title = rows[0][0] if rows and rows[0] else ""
    head_i = next((i for i, r in enumerate(rows) if r and r[0].strip().lower() == "client type"), None)
    if head_i is None:
        raise ProviderUnavailable("unexpected participant-OI layout")
    head = [h.strip().lower() for h in rows[head_i]]
    col = lambda name: head.index(name)  # noqa: E731
    out = {}
    for r in rows[head_i + 1:]:
        if not r or r[0].strip() not in PARTICIPANTS:
            continue
        g = lambda name: int(_num(r[col(name)]) or 0)  # noqa: E731
        fl, fs = g("future index long"), g("future index short")
        cl, ps = g("option index call long"), g("option index put short")
        cs, pl = g("option index call short"), g("option index put long")
        out[r[0].strip()] = {"fut_long": fl, "fut_short": fs, "fut_net": fl - fs,
                             "fut_long_pct": round(fl / (fl + fs) * 100, 1) if fl + fs else None,
                             "call_long": cl, "call_short": cs, "put_long": pl, "put_short": ps,
                             "options_bias": (cl + ps) - (cs + pl)}  # >0: bullish-leaning option book
    m = re.search(r"as on ([A-Za-z]{3} \d{1,2}, \d{4})", title)
    return {"date": dt.datetime.strptime(m.group(1), "%b %d, %Y").date().isoformat() if m else None, "participants": out}


# ------------------------------------------------------------------------------------------ fetching


class IndiaData:
    def __init__(self, nse: NseClient, cache_dir: Path | None = None):
        self.nse = nse
        self.cache_dir = cache_dir or (nse.cache_dir / "daily" if nse.cache_dir else None)

    def fii_dii(self) -> dict[str, Any]:
        return self.nse._cached("fii_dii", 15 * 60, lambda: parse_fii_dii(self.nse._json("/api/fiidiiTradeReact")))

    def deals(self) -> dict[str, Any]:
        return self.nse._cached("deals", 15 * 60, lambda: parse_large_deals(self.nse._json("/api/snapshot-capital-market-largedeal")))

    def corporate_actions(self) -> list[dict[str, Any]]:
        return self.nse._cached("corp_actions", 3 * 3600, lambda: parse_corporate_actions(self.nse._json("/api/corporates-corporateActions", {"index": "equities"})))

    def events(self) -> list[dict[str, Any]]:
        return self.nse._cached("events", 3 * 3600, lambda: parse_events(self.nse._json("/api/event-calendar")))

    def surveillance(self) -> dict[str, dict[str, str]]:
        def load():
            out = parse_surveillance(self.nse._json("/api/reportASM"), self.nse._json("/api/reportGSM"))
            for sym in self.fo_ban()["symbols"]:
                out.setdefault(sym, {})["fo_ban"] = "In the F&O ban period: no fresh F&O positions"
            return out
        return self.nse._cached("surveillance", 3600, load)

    def fo_ban(self) -> dict[str, Any]:
        return self.nse._cached("fo_ban", 3600, lambda: parse_ban(self._archive(f"{ARCHIVES}/content/fo/fo_secban.csv")))

    def ipos(self, today: dt.date) -> dict[str, Any]:
        def load():
            j = self.nse._json
            return parse_ipos(j("/api/ipo-current-issue"), j("/api/all-upcoming-issues", {"category": "ipo"}), j("/api/public-past-issues"), today)
        return self.nse._cached(("ipos", today), 30 * 60, load)

    # --- daily files (cached on disk: a published file never changes) ------------------------------
    def _archive(self, url: str) -> str:
        self.nse._session()
        try:
            r = self.nse.client.get(url, headers={"Accept": "text/csv,*/*"})
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"NSE archive unreachable: {type(exc).__name__}") from exc
        if r.status_code == 404:
            raise NoDataAvailable(f"not published: {url.rsplit('/', 1)[-1]}")
        if r.status_code != 200 or "<html" in r.text[:200].lower():
            raise ProviderUnavailable(f"NSE archive returned HTTP {r.status_code}")
        return r.text

    def _daily_file(self, kind: str, day: dt.date, url: str) -> str:
        path = self.cache_dir / kind / f"{day:%Y%m%d}.csv" if self.cache_dir else None
        if path and path.exists():
            return path.read_text(encoding="utf-8")
        text = self._archive(url)
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part"); tmp.write_text(text, encoding="utf-8"); os.replace(tmp, path)
        return text

    def _recent_days(self, kind: str, url_for, parse, sessions: int, today: dt.date) -> list[tuple[dt.date, Any]]:
        """The last `sessions` published files, newest first (weekends and holidays are skipped)."""
        def load():
            out, day, tries = [], today, 0
            while len(out) < sessions and tries < sessions * 2 + 6:
                tries += 1
                if day.weekday() < 5:
                    try:
                        out.append((day, parse(self._daily_file(kind, day, url_for(day)))))
                    except NoDataAvailable:
                        pass  # holiday, or today's file isn't out yet
                day -= dt.timedelta(days=1)
            if not out:
                raise NoDataAvailable(f"no recent {kind} files from NSE")
            return out
        return self.nse._cached((kind, today, sessions), 30 * 60, load)

    def bhav(self, today: dt.date, sessions: int = 6) -> list[tuple[dt.date, dict[str, dict[str, Any]]]]:
        return self._recent_days("bhav", lambda d: f"{ARCHIVES}/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv", parse_bhav_full, sessions, today)

    def participant_oi(self, today: dt.date, sessions: int = 5) -> list[tuple[dt.date, dict[str, Any]]]:
        return self._recent_days("participant_oi", lambda d: f"{ARCHIVES}/content/nsccl/fao_participant_oi_{d:%d%m%Y}.csv", parse_participant_oi, sessions, today)
