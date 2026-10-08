"""BSE corporate filings: the official announcements companies file with the exchange.

Results, board meetings, dividends, allotments, insider-trading disclosures... each with the filed PDF.
Yahoo Finance carries almost no news for Indian stocks, so for NSE / BSE symbols these filings are
merged into the announcements feed (``BseFilingsProvider``). Prices still come from the market provider.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Callable

import httpx

from tools.providers.base import NoDataAvailable, ProviderError, ProviderUnavailable
from tools.providers.listings import BROWSER_HEADERS, BSE_HEADERS

ANN_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
PDF_URL = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/{name}"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
PAGE_SIZE = 50
MAX_PAGES = 3


def _when(value: Any) -> dt.datetime | None:
    """BSE timestamps are Indian local time without a zone, e.g. 2026-10-01T09:15:04.317."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value[:19]).replace(tzinfo=IST)
    except ValueError:
        return None


def _instant(item: dict[str, Any]) -> dt.datetime:
    """Sort key: news is stamped in UTC and filings in Indian time, so compare instants, not strings."""
    try:
        when = dt.datetime.fromisoformat(str(item.get("published_at")))
        return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)


def _clean(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").replace("''", "'")).strip()  # BSE doubles apostrophes


def parse_filings(payload: Any) -> list[dict[str, Any]]:
    """BSE's announcement JSON -> announcement dicts in the tool contract's shape."""
    if not isinstance(payload, dict) or not isinstance(payload.get("Table"), list):
        raise ValueError("unexpected BSE announcements payload")
    out = []
    for row in payload["Table"]:
        if not isinstance(row, dict):
            continue
        when = _when(row.get("NEWS_DT") or row.get("DT_TM"))
        subject = _clean(row.get("NEWSSUB"))
        if when is None or not subject:
            continue
        # "Announcement under Regulation 30 (LODR)-Allotment" -> "Allotment"
        title = re.sub(r"^Announcement under Regulation \d+ \(LODR\)\s*-\s*", "", subject) or subject
        category = _clean(row.get("CATEGORYNAME")) or "Announcement"
        sub = _clean(row.get("SUBCATNAME"))
        attach = _clean(row.get("ATTACHMENTNAME"))
        summary = _clean(row.get("MORE")) or _clean(row.get("HEADLINE"))
        out.append({
            "id": f"bse:{row.get('NEWSID') or row.get('XML_NAME')}",
            "published_at": when.isoformat(),
            "category": f"filing: BSE · {category}" + (f" · {sub}" if sub and sub.lower() not in title.lower() else ""),
            "title": title,
            "summary": summary[:600],
            "url": PDF_URL.format(name=attach) if attach.lower().endswith(".pdf") else _clean(row.get("NSURL")) or None,
        })
    return out


class BseFilings:
    """Fetches a company's filings by BSE scrip code."""

    def __init__(self, client: httpx.Client | None = None, timeout_s: float = 8.0):
        self.client = client or httpx.Client(headers={**BROWSER_HEADERS, **BSE_HEADERS}, timeout=timeout_s)

    def fetch(self, scrip_code: str, lookback_days: int) -> list[dict[str, Any]]:
        today = dt.datetime.now(IST).date()
        params = {"strCat": -1, "strPrevDate": (today - dt.timedelta(days=lookback_days)).strftime("%Y%m%d"),
                  "strScrip": scrip_code, "strSearch": "P", "strToDate": today.strftime("%Y%m%d"), "strType": "C",
                  "subcategory": -1}
        items: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            try:
                r = self.client.get(ANN_URL, params={"pageno": page, **params})
            except httpx.HTTPError as exc:
                raise ProviderUnavailable(f"BSE announcements request failed: {type(exc).__name__}") from exc
            if r.status_code != 200:
                raise ProviderUnavailable(f"BSE announcements returned HTTP {r.status_code}")
            try:
                payload = r.json()
            except ValueError as exc:
                raise ProviderUnavailable("BSE announcements response was not JSON") from exc
            batch = parse_filings(payload)
            items += batch
            total = ((payload.get("Table1") or [{}])[0] or {}).get("ROWCNT") or 0
            if len(payload.get("Table") or []) < PAGE_SIZE or page * PAGE_SIZE >= total:
                break
        return items


class BseFilingsProvider:
    """Wraps the market provider: for NSE / BSE symbols, announcements = the provider's news + BSE filings.

    ``code_for`` maps a symbol (RELIANCE.NS or RELIANCE.BO) to its BSE scrip code, or None. Everything
    else is passed straight through to the wrapped provider.
    """

    def __init__(self, inner: Any, filings: BseFilings, code_for: Callable[[str], str | None]):
        self.inner, self.filings, self.code_for = inner, filings, code_for
        self.name = inner.name

    def __getattr__(self, attr: str) -> Any:
        return getattr(self.inner, attr)

    def fetch_announcements(self, symbol: str, lookback_days: int) -> dict[str, Any]:
        code = self.code_for(symbol) if symbol.endswith((".NS", ".BO")) else None
        if not code:
            return self.inner.fetch_announcements(symbol, lookback_days)
        base: dict[str, Any] | None = None
        base_error: ProviderError | None = None
        try:
            base = self.inner.fetch_announcements(symbol, lookback_days)
        except (NoDataAvailable, ProviderUnavailable) as exc:
            base_error = exc
        try:
            filings = self.filings.fetch(code, lookback_days)
        except ProviderError:
            if base is not None:
                return base  # BSE is down: the provider's own news still stands
            raise base_error or ProviderUnavailable("BSE announcements unavailable") from None
        data = (base or {}).get("data")
        news = (data.get("items") or []) if isinstance(data, dict) else []
        items = sorted([*news, *filings], key=_instant, reverse=True)
        meta = dict((base or {}).get("meta") or {})
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        meta.update({"source_id": f"{meta.get('source_id', '')} + {ANN_URL}?strScrip={code}".lstrip(" +"),
                     "url": meta.get("url") or f"https://www.bseindia.com/corporates/ann.html?scrip={code}",
                     "observed_at": meta.get("observed_at") or now})
        return {"meta": meta, "data": {"symbol": symbol, "items": items}}

