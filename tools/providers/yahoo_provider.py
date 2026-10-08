"""Real market data from Yahoo Finance's public JSON endpoints (no API key).

* prices:  GET /v8/finance/chart/{symbol}?interval=1d&period1=..&period2=..
* news:    GET /v1/finance/search?q={symbol}&newsCount=N

These are unofficial endpoints: they can change shape or rate-limit without
notice. That is exactly what the tool contract is for -- shape changes surface
as ``MalformedResponseError`` and HTTP 429/5xx as ``DataSourceUnavailableError``.

``yfinance`` is deliberately not used: it pulls in pandas/numpy, whose native
DLLs are blocked on some locked-down Windows machines, and plain httpx is all
that is needed here.

Normalisation done here (and only here):
  * bars whose OHLC values are *all* null (non-trading placeholders) are dropped;
    partially-null bars are passed through so the tool layer rejects them;
  * duplicate bars for the same exchange-local date keep the last one;
  * prices are rounded to 4 decimals (Yahoo returns float32 artefacts like 329.3999938).

Timestamps:
  * the latest bar's ``observed_at`` is Yahoo's ``regularMarketTime`` (the time of
    the last regular-session trade -- live during market hours);
  * an older bar's ``observed_at`` is that day's regular-session close;
  * the news feed's ``observed_at`` is the fetch time: it is a live feed, so what
    we observed is its state *now*. Each article keeps its own publish time.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from typing import Any

import httpx

from tools.providers.base import NoDataAvailable, ProviderError, ProviderUnavailable, SymbolNotFound

BASE_URL = "https://query1.finance.yahoo.com"
FALLBACK_URL = "https://query2.finance.yahoo.com"  # same API, separate rate-limit bucket
_HEADERS = {
    # Yahoo rejects requests without a browser-like user agent.
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json",
}
_DEFAULT_CLOSE = dt.time(16, 0)

# range -> (Yahoo range fetched, bar interval, visible span in days or None for everything fetched).
# Daily and longer ranges fetch extra history so moving averages are already warm at the left edge.
CHART_SPECS: dict[str, tuple[str, str, int | None]] = {
    "1D": ("1d", "5m", None), "1W": ("5d", "15m", None), "1M": ("3mo", "60m", 31),
    "3M": ("1y", "1d", 92), "6M": ("2y", "1d", 183), "1Y": ("2y", "1d", 366),
    "5Y": ("10y", "1wk", 5 * 366), "ALL": ("max", "1mo", None),
}
INTRADAY = {"5m", "15m", "60m"}
# terminal interval -> (Yahoo range, Yahoo interval): the most history Yahoo serves at each bar size.
BAR_SPECS: dict[str, tuple[str, str]] = {
    "1m": ("7d", "1m"), "5m": ("60d", "5m"), "15m": ("60d", "15m"), "1h": ("2y", "60m"), "1D": ("max", "1d"),
}


def _utc(ts: int | float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)


def _round(value: Any) -> Any:
    return round(float(value), 4) if isinstance(value, (int, float)) else value


class YahooFinanceProvider:
    name = "yahoo_finance"

    def __init__(self, *, timeout_s: float = 8.0, client: httpx.Client | None = None,
                 news_count: int = 12):
        self._client = client or httpx.Client(base_url=BASE_URL, headers=_HEADERS, timeout=timeout_s)
        self._news_count = news_count
        # Yahoo normally answers in well under a second. A request that stalls longer is retried once on
        # the other host instead of waiting out the whole budget; both attempts fit inside timeout_s.
        self._first_timeout = httpx.Timeout(min(3.0, timeout_s * 0.4), connect=min(2.0, timeout_s * 0.25))
        self._retry_timeout = httpx.Timeout(max(1.0, timeout_s - self._first_timeout.read - 0.5))
        self._prev_closes: dict[tuple[str, str], float | None] = {}
        self._crumb_value: str | None = None
        self._crumb_at = 0.0
        self._crumb_lock = threading.Lock()

    # -- HTTP --------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any], symbol: str) -> dict[str, Any]:
        try:
            try:
                response = self._client.get(path, params=params, timeout=self._first_timeout)
            except httpx.TimeoutException:
                response = self._client.get(FALLBACK_URL + path, params=params, timeout=self._retry_timeout)
            else:
                if response.status_code == 429 or response.status_code >= 500:  # rate limit or a bad edge: other host
                    response = self._client.get(FALLBACK_URL + path, params=params, timeout=self._retry_timeout)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"Yahoo Finance request failed: {type(exc).__name__}: {exc}") from exc
        if response.status_code == 404:
            raise SymbolNotFound(f"Yahoo Finance has no data for {symbol!r}")
        if response.status_code == 429 or response.status_code >= 500:
            raise ProviderUnavailable(f"Yahoo Finance returned HTTP {response.status_code}")
        if response.status_code != 200:
            raise ProviderUnavailable(f"Yahoo Finance returned HTTP {response.status_code}: "
                                      f"{response.text[:200]}")
        try:
            return response.json()
        except ValueError:
            # Not JSON at all -> let the tool layer flag it as malformed.
            return {"_raw": response.text[:500]}

    def _chart(self, symbol: str, start: dt.datetime, end: dt.datetime) -> tuple[dict | None, str, Any]:
        params = {"interval": "1d", "period1": int(start.timestamp()), "period2": int(end.timestamp()),
                  "includePrePost": "false", "events": ""}
        return self._chart_request(symbol, params)

    def _chart_request(self, symbol: str, params: dict[str, Any]) -> tuple[dict | None, str, Any]:
        """Returns (result, source_url, raw_payload); result is None if the shape is unrecognised."""
        payload = self._get(f"/v8/finance/chart/{symbol}", params, symbol)
        chart = payload.get("chart") if isinstance(payload, dict) else None
        error = chart.get("error") if isinstance(chart, dict) else None
        if error:
            if "not found" in str(error).lower() or "delisted" in str(error).lower():
                description = error.get("description", error) if isinstance(error, dict) else error
                raise SymbolNotFound(f"Yahoo Finance: {description}")
            raise ProviderUnavailable(f"Yahoo Finance chart error: {error}")
        results = chart.get("result") if isinstance(chart, dict) else None
        source_url = f"{BASE_URL}/v8/finance/chart/{symbol}?interval=1d"
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            return None, source_url, payload
        return results[0], source_url, payload

    @staticmethod
    def _unrecognised(payload: Any) -> dict[str, Any]:
        # Hand the unexpected payload to the tool layer, which reports MALFORMED_RESPONSE.
        return {"meta": {}, "data": payload}

    # -- bar extraction ------------------------------------------------------

    @staticmethod
    def _bars(result: dict[str, Any]) -> list[dict[str, Any]]:
        meta = result.get("meta") or {}
        offset = dt.timedelta(seconds=int(meta.get("gmtoffset") or 0))
        timestamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        by_date: dict[str, dict[str, Any]] = {}
        for i, ts in enumerate(timestamps):
            row = {k: (quote.get(k) or [None] * len(timestamps))[i]
                   for k in ("open", "high", "low", "close", "volume")}
            if all(row[k] is None for k in ("open", "high", "low", "close")):
                continue
            day = (_utc(ts) + offset).date().isoformat()
            by_date[day] = {"date": day, **{k: _round(v) for k, v in row.items()}}
            if isinstance(row["volume"], (int, float)):
                by_date[day]["volume"] = int(row["volume"])
        return [by_date[d] for d in sorted(by_date)]

    @staticmethod
    def _observed_at(result: dict[str, Any], day: str | None) -> str | None:
        """UTC ISO timestamp at which the bar for ``day`` was last observed."""
        if not day:
            return None
        meta = result.get("meta") or {}
        offset = dt.timedelta(seconds=int(meta.get("gmtoffset") or 0))
        market_time = meta.get("regularMarketTime")
        if isinstance(market_time, (int, float)):
            last_trade = _utc(market_time)
            if (last_trade + offset).date().isoformat() == day:
                return last_trade.isoformat()
        regular = ((meta.get("currentTradingPeriod") or {}).get("regular") or {})
        close_local = (_utc(regular["end"]) + offset).time() if "end" in regular else _DEFAULT_CLOSE
        close = dt.datetime.combine(dt.date.fromisoformat(day), close_local) - offset
        return close.replace(tzinfo=dt.timezone.utc).isoformat()

    # -- endpoints -------------------------------------------------------------

    def fetch_quote(self, symbol: str, on_date: dt.date | None) -> dict[str, Any]:
        now = dt.datetime.now(dt.timezone.utc)
        if on_date is None:
            start, end = now - dt.timedelta(days=10), now
        else:
            day_start = dt.datetime.combine(on_date, dt.time(), tzinfo=dt.timezone.utc)
            start, end = day_start - dt.timedelta(days=3), min(day_start + dt.timedelta(days=2), now)
        result, url, raw = self._chart(symbol, start, end)
        if result is None:
            return self._unrecognised(raw)
        bars = self._bars(result)
        if on_date is None:
            if not bars:
                raise NoDataAvailable(f"Yahoo Finance returned no recent bars for {symbol}")
            bar = bars[-1]
        else:
            bar = next((b for b in bars if b["date"] == on_date.isoformat()), None)
            if bar is None:
                raise NoDataAvailable(f"no trading bar for {symbol} on {on_date.isoformat()}")
        meta = result.get("meta") or {}
        return {
            "meta": {"source_id": url + (f"&date={on_date.isoformat()}" if on_date else "&latest"),
                     "url": f"https://finance.yahoo.com/quote/{symbol}/history",
                     "observed_at": self._observed_at(result, bar.get("date"))},
            "data": {"symbol": meta.get("symbol"), "currency": meta.get("currency"), "bar": bar},
        }

    def fetch_price_history(self, symbol: str, lookback_days: int) -> dict[str, Any]:
        now = dt.datetime.now(dt.timezone.utc)
        result, url, raw = self._chart(symbol, now - dt.timedelta(days=lookback_days), now)
        if result is None:
            return self._unrecognised(raw)
        bars = self._bars(result)
        if not bars:
            raise NoDataAvailable(f"Yahoo Finance returned no bars for {symbol} in {lookback_days}d")
        meta = result.get("meta") or {}
        return {
            "meta": {"source_id": f"{url}&lookback={lookback_days}d",
                     "url": f"https://finance.yahoo.com/quote/{symbol}/history",
                     "observed_at": self._observed_at(result, bars[-1].get("date"))},
            "data": {"symbol": meta.get("symbol"), "currency": meta.get("currency"),
                     "name": meta.get("longName") or meta.get("shortName"), "bars": bars},
        }

    @staticmethod
    def _candles(result: dict[str, Any]) -> list[dict[str, Any]]:
        timestamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        col = {k: quote.get(k) or [None] * len(timestamps) for k in ("open", "high", "low", "close", "volume")}
        out = []
        for i, ts in enumerate(timestamps):
            close = col["close"][i]
            if close is None or close <= 0:
                continue  # empty bucket (no trades) or a gap in the feed
            o, h, lo = (col[k][i] or close for k in ("open", "high", "low"))
            vol = col["volume"][i]
            out.append({"t": _utc(ts).isoformat(), "open": _round(o), "high": _round(max(h, o, close)),
                        "low": _round(min(lo, o, close)), "close": _round(close),
                        "volume": int(vol) if isinstance(vol, (int, float)) and vol >= 0 else None})
        return out

    def fetch_chart(self, symbol: str, chart_range: str) -> dict[str, Any]:
        """Candles sized for the range: 5-minute bars for 1D up to monthly bars for the full history."""
        fetch_range, interval, span_days = CHART_SPECS[chart_range]
        params = {"interval": interval, "range": fetch_range, "includePrePost": "false", "events": ""}
        result, url, raw = self._chart_request(symbol, params)
        if result is None:
            return self._unrecognised(raw)
        bars = self._candles(result)
        if chart_range == "ALL" and len(bars) < 60:  # young listing: monthly bars would be too few to read
            for finer in ("1wk", "1d"):
                params["interval"] = interval = finer
                result, url, raw = self._chart_request(symbol, params)
                if result is None:
                    return self._unrecognised(raw)
                bars = self._candles(result)
                if len(bars) >= 60:
                    break
        if not bars:
            raise NoDataAvailable(f"Yahoo Finance returned no {interval} bars for {symbol} ({chart_range})")
        meta = result.get("meta") or {}
        interval = meta.get("dataGranularity") or interval  # Yahoo sends weekly bars for short histories
        last_ts = dt.datetime.fromisoformat(bars[-1]["t"])
        if span_days is None:
            visible_from = dt.datetime.fromisoformat(bars[0]["t"])
        else:
            visible_from = last_ts - dt.timedelta(days=span_days)
        before = [b for b in bars if dt.datetime.fromisoformat(b["t"]) < visible_from]
        if chart_range in ("1D", "1W"):
            prev_close = _round(meta.get("chartPreviousClose") or meta.get("previousClose"))
        else:
            prev_close = before[-1]["close"] if before else None
        market_time = meta.get("regularMarketTime")
        observed = _utc(market_time).isoformat() if isinstance(market_time, (int, float)) else bars[-1]["t"]
        return {
            "meta": {"source_id": f"{BASE_URL}/v8/finance/chart/{symbol}?interval={interval}&range={fetch_range}",
                     "url": f"https://finance.yahoo.com/quote/{symbol}/chart", "observed_at": observed},
            "data": {"symbol": meta.get("symbol") or symbol, "name": meta.get("longName") or meta.get("shortName"),
                     "currency": meta.get("currency"), "range": chart_range, "interval": interval,
                     "intraday": interval in INTRADAY, "previous_close": prev_close,
                     "visible_from": visible_from.isoformat(), "bars": bars},
        }

    # -- company profile (needs a session crumb) -------------------------------

    def _crumb(self, refresh: bool = False) -> str:
        """Yahoo's quoteSummary wants a cookie + crumb pair; get one once and reuse it for hours."""
        with self._crumb_lock:
            if self._crumb_value and not refresh and time.time() - self._crumb_at < 6 * 3600:
                return self._crumb_value
            try:
                anything = {"Accept": "*/*"}  # the crumb is plain text: "Accept: application/json" gets a 406
                self._client.get("https://fc.yahoo.com", headers=anything, timeout=self._first_timeout)  # sets the A3 cookie (404 is normal)
                resp = self._client.get("/v1/test/getcrumb", headers=anything, timeout=self._retry_timeout)
            except httpx.HTTPError as exc:
                raise ProviderUnavailable(f"Yahoo Finance session failed: {type(exc).__name__}") from exc
            crumb = resp.text.strip()
            if resp.status_code != 200 or not crumb or len(crumb) > 64 or "<" in crumb:
                raise ProviderUnavailable(f"Yahoo Finance gave no session crumb (HTTP {resp.status_code})")
            self._crumb_value, self._crumb_at = crumb, time.time()
            return crumb

    def fetch_company(self, symbol: str) -> dict[str, Any]:
        """Business description, website, headcount, headquarters and key people."""
        path = f"/v10/finance/quoteSummary/{symbol}"
        modules = "assetProfile,summaryProfile,quoteType,price"
        for attempt in range(2):
            try:
                payload = self._get(path, {"modules": modules, "crumb": self._crumb(refresh=attempt > 0)}, symbol)
                break
            except ProviderUnavailable as exc:
                if attempt or "HTTP 401" not in str(exc) and "HTTP 403" not in str(exc):
                    raise  # only an expired crumb is worth one retry
        summary = payload.get("quoteSummary") if isinstance(payload, dict) else None
        if not isinstance(summary, dict):
            return self._unrecognised(payload)
        if summary.get("error"):
            err = summary["error"]
            if "not found" in str(err).lower():
                raise SymbolNotFound(f"Yahoo Finance has no profile for {symbol!r}")
            raise ProviderUnavailable(f"Yahoo Finance profile error: {err}")
        result = (summary.get("result") or [None])[0]
        if not isinstance(result, dict):
            return self._unrecognised(payload)
        ap = result.get("assetProfile") or {}
        sp = result.get("summaryProfile") or {}
        qt = result.get("quoteType") or {}
        price = result.get("price") or {}
        raw = lambda v: v.get("raw") if isinstance(v, dict) else v  # noqa: E731 - Yahoo wraps numbers
        officers = [{"name": " ".join(o["name"].split()), "title": " ".join((o.get("title") or "").split()) or None,
                     "year_born": o.get("yearBorn") if isinstance(o.get("yearBorn"), int) else None}
                    for o in (ap.get("companyOfficers") or []) if isinstance(o, dict) and (o.get("name") or "").strip()]
        employees = ap.get("fullTimeEmployees")
        return {
            "meta": {"source_id": f"{BASE_URL}{path}?modules={modules}",
                     "url": f"https://finance.yahoo.com/quote/{symbol}/profile",
                     "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()},
            "data": {
                "symbol": qt.get("symbol") or symbol,
                "name": price.get("longName") or qt.get("longName") or price.get("shortName") or qt.get("shortName"),
                "description": (ap.get("longBusinessSummary") or sp.get("description") or "").strip() or None,
                "website": ap.get("website") or sp.get("website"),
                "sector": ap.get("sector") or None, "industry": ap.get("industry") or None,
                "employees": employees if isinstance(employees, int) and employees >= 0 else None,
                "city": ap.get("city") or None, "state": ap.get("state") or None, "country": ap.get("country") or None,
                "market_cap": raw(price.get("marketCap")), "currency": price.get("currency"),
                "quote_type": qt.get("quoteType"), "officers": officers[:6],
            },
        }

    def _previous_close(self, symbol: str, meta: dict[str, Any]) -> float | None:
        """The previous session's close, read from the daily candles.

        Yahoo's ``previousClose`` / ``chartPreviousClose`` on intraday requests is sometimes a session
        stale for indices (NIFTY 50 reported the close from two sessions back), which made the day's
        change wrong. The daily candles are right, so they decide; one small request per symbol per day.
        """
        fallback = _round(meta.get("previousClose") or meta.get("chartPreviousClose"))
        market_time = meta.get("regularMarketTime")
        if not isinstance(market_time, (int, float)):
            return fallback
        offset = dt.timedelta(seconds=int(meta.get("gmtoffset") or 0))
        session = (_utc(market_time) + offset).date().isoformat()
        key = (symbol, session)
        if key not in self._prev_closes:
            try:
                result, _, _ = self._chart_request(symbol, {"interval": "1d", "range": "5d", "includePrePost": "false"})
                bars = self._bars(result) if result else []
                prior = [b for b in bars if b["date"] < session and b.get("close")]
                value = prior[-1]["close"] if prior else None
            except ProviderError:
                return fallback  # don't remember a failure; try again next time
            if len(self._prev_closes) > 5000:
                self._prev_closes.clear()
            self._prev_closes[key] = value
        return self._prev_closes[key] or fallback

    def fetch_bars(self, symbol: str, interval: str) -> dict[str, Any]:
        """Candles at one interval for the terminal chart."""
        fetch_range, yahoo_interval = BAR_SPECS[interval]
        params = {"interval": yahoo_interval, "range": fetch_range, "includePrePost": "false", "events": ""}
        if fetch_range == "max":  # range=max quietly switches to monthly bars; an explicit window stays daily
            params = {"interval": yahoo_interval, "period1": 0, "period2": int(time.time()), "includePrePost": "false", "events": ""}
        result, url, raw = self._chart_request(symbol, params)
        if result is None:
            return self._unrecognised(raw)
        bars = self._candles(result)
        if not bars:
            raise NoDataAvailable(f"Yahoo Finance returned no {yahoo_interval} bars for {symbol}")
        meta = result.get("meta") or {}
        market_time = meta.get("regularMarketTime")
        return {
            "meta": {"source_id": f"{BASE_URL}/v8/finance/chart/{symbol}?interval={yahoo_interval}&range={fetch_range}",
                     "url": f"https://finance.yahoo.com/quote/{symbol}/chart",
                     "observed_at": _utc(market_time).isoformat() if isinstance(market_time, (int, float)) else bars[-1]["t"]},
            "data": {"symbol": meta.get("symbol") or symbol, "name": meta.get("longName") or meta.get("shortName"),
                     "currency": meta.get("currency"), "interval": interval,
                     "previous_close": self._previous_close(symbol, meta) if interval != "1D" else None,
                     "bars": bars},
        }

    _SERIES = {"TotalRevenue": "revenue", "OperatingIncome": "operating_income", "NetIncome": "net_income",
               "DilutedEPS": "eps", "StockholdersEquity": "equity", "TotalDebt": "debt", "FreeCashFlow": "free_cash_flow"}

    def _statements(self, symbol: str, crumb: str) -> tuple[list[dict], list[dict]]:
        """Annual (up to 4 years) and quarterly (up to 5 quarters) results from Yahoo's fundamentals timeseries."""
        types = ",".join(f"{freq}{k}" for freq in ("annual", "quarterly") for k in self._SERIES)
        payload = self._get(f"/ws/fundamentals-timeseries/v1/finance/timeseries/{symbol}",
                            {"type": types, "period1": 1262304000, "period2": int(time.time()), "crumb": crumb}, symbol)
        out: dict[str, dict[str, dict]] = {"annual": {}, "quarterly": {}}
        for series in ((payload.get("timeseries") or {}).get("result") or []) if isinstance(payload, dict) else []:
            key = ((series.get("meta") or {}).get("type") or [""])[0]
            freq = "annual" if key.startswith("annual") else "quarterly" if key.startswith("quarterly") else None
            field = self._SERIES.get(key.removeprefix("annual").removeprefix("quarterly"))
            if not freq or not field:
                continue
            for point in series.get(key) or []:
                if not isinstance(point, dict) or not point.get("asOfDate"):
                    continue
                value = (point.get("reportedValue") or {}).get("raw")
                row = out[freq].setdefault(point["asOfDate"], {"period_end": point["asOfDate"]})
                if isinstance(value, (int, float)):
                    row[field] = value
        ordered = lambda rows: [rows[k] for k in sorted(rows)]  # noqa: E731
        return ordered(out["annual"])[-4:], ordered(out["quarterly"])[-5:]

    def fetch_fundamentals(self, symbol: str, statements: bool = True) -> dict[str, Any]:
        """Ratios (quoteSummary) and, optionally, reported results (fundamentals timeseries)."""
        modules = "summaryDetail,defaultKeyStatistics,financialData,assetProfile,price"
        path = f"/v10/finance/quoteSummary/{symbol}"
        for attempt in range(2):
            try:
                crumb = self._crumb(refresh=attempt > 0)
                payload = self._get(path, {"modules": modules, "crumb": crumb}, symbol)
                break
            except ProviderUnavailable as exc:
                if attempt or "HTTP 401" not in str(exc) and "HTTP 403" not in str(exc):
                    raise
        summary = payload.get("quoteSummary") if isinstance(payload, dict) else None
        if not isinstance(summary, dict):
            return self._unrecognised(payload)
        if summary.get("error"):
            if "not found" in str(summary["error"]).lower():
                raise SymbolNotFound(f"Yahoo Finance has no fundamentals for {symbol!r}")
            raise ProviderUnavailable(f"Yahoo Finance fundamentals error: {summary['error']}")
        r = (summary.get("result") or [None])[0]
        if not isinstance(r, dict):
            return self._unrecognised(payload)
        def num(module: str, key: str) -> float | None:
            v = (r.get(module) or {}).get(key)
            v = v.get("raw") if isinstance(v, dict) else v
            return float(v) if isinstance(v, (int, float)) else None
        sd, ks, fd = "summaryDetail", "defaultKeyStatistics", "financialData"
        shares, book, ni = num(ks, "sharesOutstanding"), num(ks, "bookValue"), num(ks, "netIncomeToCommon")
        roe = num(fd, "returnOnEquity")
        if roe is None and shares and book and ni is not None and book > 0:
            roe = ni / (book * shares)  # Yahoo leaves ROE blank for many Indian stocks
        de = num(fd, "debtToEquity")
        annual, quarterly = self._statements(symbol, crumb) if statements else ([], [])
        price, prof = r.get("price") or {}, r.get("assetProfile") or {}
        return {
            "meta": {"source_id": f"{BASE_URL}{path}?modules={modules} + fundamentals-timeseries",
                     "url": f"https://finance.yahoo.com/quote/{symbol}/key-statistics",
                     "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()},
            "data": {
                "symbol": symbol, "name": price.get("longName") or price.get("shortName"), "currency": price.get("currency"),
                "sector": prof.get("sector") or None, "industry": prof.get("industry") or None,
                "market_cap": num(sd, "marketCap") or num("price", "marketCap"),
                "pe": num(sd, "trailingPE"), "forward_pe": num(sd, "forwardPE"), "pb": num(ks, "priceToBook"),
                "ps": num(sd, "priceToSalesTrailing12Months"), "ev_ebitda": num(ks, "enterpriseToEbitda"),
                "eps": num(ks, "trailingEps"), "book_value": book,
                "dividend_yield": num(sd, "dividendYield") or num(sd, "trailingAnnualDividendYield"), "payout_ratio": num(sd, "payoutRatio"),
                "roe": roe, "net_margin": num(fd, "profitMargins"), "operating_margin": num(fd, "operatingMargins"),
                "gross_margin": num(fd, "grossMargins"), "debt_to_equity": de / 100 if de is not None else None,
                "revenue_growth": num(fd, "revenueGrowth"), "earnings_growth": num(fd, "earningsGrowth"),
                "beta": num(sd, "beta"), "year_change": num(ks, "52WeekChange"),
                "annual": annual, "quarterly": quarterly,
            },
        }

    def fetch_daily_snapshot(self, symbol: str) -> dict[str, Any]:
        """One request: a month of daily bars + meta (52-week range, name, last trade time)."""
        result, url, raw = self._chart_request(symbol, {"interval": "1d", "range": "1mo", "includePrePost": "false"})
        if result is None:
            return self._unrecognised(raw)
        meta = result.get("meta") or {}
        bars = self._bars(result)
        if not bars:
            raise NoDataAvailable(f"Yahoo Finance returned no daily bars for {symbol}")
        last, prev = bars[-1], (bars[-2] if len(bars) > 1 else None)
        prior_vols = [b["volume"] for b in bars[:-1][-20:] if isinstance(b.get("volume"), int) and b["volume"] > 0]
        market_time = meta.get("regularMarketTime")
        observed = _utc(market_time).isoformat() if isinstance(market_time, (int, float)) else self._observed_at(result, last["date"])
        return {
            "meta": {"source_id": f"{url}&range=1mo&snapshot", "url": f"https://finance.yahoo.com/quote/{symbol}",
                     "observed_at": observed},
            "data": {
                "symbol": meta.get("symbol"), "name": meta.get("longName") or meta.get("shortName"),
                "exchange": meta.get("fullExchangeName") or meta.get("exchangeName"), "currency": meta.get("currency"),
                "price": _round(meta.get("regularMarketPrice")) or last.get("close"),
                "previous_close": prev.get("close") if prev else _round(meta.get("chartPreviousClose")),
                "volume": last.get("volume") if isinstance(last.get("volume"), int) else None,
                "avg_volume": sum(prior_vols) / len(prior_vols) if prior_vols else None,
                "fifty_two_week_high": _round(meta.get("fiftyTwoWeekHigh")),
                "fifty_two_week_low": _round(meta.get("fiftyTwoWeekLow")),
                "session_date": last["date"], "market_time": observed,
                "closes": [b["close"] for b in bars if b.get("close") is not None][-22:],
            },
        }

    def fetch_profile(self, symbol: str) -> dict[str, Any]:
        """Name, sector and industry from the search endpoint."""
        payload = self._get("/v1/finance/search", {"q": symbol, "quotesCount": 5, "newsCount": 0}, symbol)
        if not isinstance(payload, dict) or not isinstance(payload.get("quotes", []), list):
            return self._unrecognised(payload)
        match = self._profile_match(payload.get("quotes", []), symbol)
        base = symbol.split(".")[0]
        if match is None and base != symbol:
            # Search is flaky for some suffixed tickers (e.g. HDFCBANK.NS): retry with the bare ticker and
            # accept the same company's other Indian listing -- sector and industry are identical.
            retry = self._get("/v1/finance/search", {"q": base, "quotesCount": 8, "newsCount": 0}, symbol)
            if isinstance(retry, dict) and isinstance(retry.get("quotes", []), list):
                match = self._profile_match(retry.get("quotes", []), symbol)
        if match is None:
            raise NoDataAvailable(f"Yahoo Finance search has no profile for {symbol}")
        return {
            "meta": {"source_id": f"{BASE_URL}/v1/finance/search?q={symbol}&profile",
                     "url": f"https://finance.yahoo.com/quote/{symbol}/profile",
                     "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()},
            "data": {"symbol": symbol, "name": match.get("longname") or match.get("shortname"),
                     "sector": match.get("sectorDisp") or match.get("sector"),
                     "industry": match.get("industryDisp") or match.get("industry"),
                     "quote_type": match.get("quoteType"), "exchange": match.get("exchDisp")},
        }

    @staticmethod
    def _profile_match(quotes: list[Any], symbol: str) -> dict[str, Any] | None:
        rows = [q for q in quotes if isinstance(q, dict)]
        exact = next((q for q in rows if str(q.get("symbol", "")).upper() == symbol), None)
        if exact is not None or not symbol.endswith((".NS", ".BO")):
            return exact
        base = symbol.rsplit(".", 1)[0]
        return next((q for q in rows if str(q.get("symbol", "")).upper() in (f"{base}.NS", f"{base}.BO")), None)

    def fetch_live(self, symbol: str) -> dict[str, Any]:
        """Today's regular-session 1-minute path plus the latest trade (``regularMarketTime``)."""
        result, url, raw = self._chart_request(
            symbol, {"interval": "1m", "range": "1d", "includePrePost": "false"})
        if result is None:
            return self._unrecognised(raw)
        meta = result.get("meta") or {}
        timestamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        closes = quote.get("close") or []
        opens = [o for o in (quote.get("open") or []) if o is not None]
        points = [{"t": _utc(ts).isoformat(), "price": _round(c)}
                  for ts, c in zip(timestamps, closes) if c is not None]
        price, market_time = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
        has_time = isinstance(market_time, (int, float))
        if has_time and isinstance(price, (int, float)):
            if not points or _utc(market_time) > dt.datetime.fromisoformat(points[-1]["t"]):
                points.append({"t": _utc(market_time).isoformat(), "price": _round(price)})
        regular = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
        now = dt.datetime.now(dt.timezone.utc).timestamp()
        is_open = bool(regular) and regular.get("start", 0) <= now <= regular.get("end", 0)
        return {
            "meta": {"source_id": f"{url}&interval=1m&range=1d",
                     "url": f"https://finance.yahoo.com/quote/{symbol}",
                     "observed_at": _utc(market_time).isoformat() if has_time else None},
            "data": {
                "symbol": meta.get("symbol"), "currency": meta.get("currency"),
                "name": meta.get("longName") or meta.get("shortName"),
                "exchange": meta.get("fullExchangeName") or meta.get("exchangeName"),
                "exchange_timezone": meta.get("exchangeTimezoneName"),
                "open": _round(opens[0]) if opens else None,
                "volume": meta.get("regularMarketVolume"),
                "fifty_two_week_high": _round(meta.get("fiftyTwoWeekHigh")),
                "fifty_two_week_low": _round(meta.get("fiftyTwoWeekLow")),
                "price": _round(price),
                "market_time": _utc(market_time).isoformat() if has_time else None,
                "previous_close": self._previous_close(symbol, meta),
                "day_high": _round(meta.get("regularMarketDayHigh")),
                "day_low": _round(meta.get("regularMarketDayLow")),
                "market_state": "open" if is_open else "closed",
                "session_start": _utc(regular["start"]).isoformat() if "start" in regular else None,
                "session_end": _utc(regular["end"]).isoformat() if "end" in regular else None,
                "points": points,
            },
        }

    @staticmethod
    def _pictures(article: dict[str, Any]) -> tuple[str | None, str | None]:
        """(full-size, smallest) picture URLs from Yahoo's thumbnail resolutions, if the article has any."""
        sizes = [r for r in ((article.get("thumbnail") or {}).get("resolutions") or [])
                 if isinstance(r, dict) and isinstance(r.get("url"), str) and r["url"].startswith("https://")]
        if not sizes:
            return None, None
        area = lambda r: (r.get("width") or 0) * (r.get("height") or 0)  # noqa: E731
        sizes.sort(key=area)
        return sizes[-1]["url"], sizes[0]["url"]

    def fetch_announcements(self, symbol: str, lookback_days: int) -> dict[str, Any]:
        now = dt.datetime.now(dt.timezone.utc)
        payload = self._get("/v1/finance/search",
                            {"q": symbol, "newsCount": self._news_count, "quotesCount": 1}, symbol)
        if not isinstance(payload, dict) or not isinstance(payload.get("news", []), list):
            return self._unrecognised(payload)
        news = payload.get("news")
        items = []
        cutoff = now - dt.timedelta(days=lookback_days)
        for article in news or []:
            if not isinstance(article, dict):
                items.append(article)  # let the tool layer reject it
                continue
            related = article.get("relatedTickers")
            if related and symbol not in related:
                continue  # search hit about a different company
            published = article.get("providerPublishTime")
            published_at = _utc(published) if isinstance(published, (int, float)) else None
            if published_at is not None and published_at < cutoff:
                continue
            publisher = article.get("publisher") or "unknown publisher"
            image, thumb = self._pictures(article)
            items.append({
                "id": article.get("uuid"),
                "published_at": published_at.isoformat() if published_at else published,
                "category": f"news: {publisher}",
                "title": article.get("title"),
                "summary": "",
                "url": article.get("link"),
                "image_url": image,
                "thumbnail_url": thumb,
            })
        return {
            "meta": {"source_id": f"{BASE_URL}/v1/finance/search?q={symbol}&news",
                     "url": f"https://finance.yahoo.com/quote/{symbol}/news",
                     "observed_at": now.isoformat()},
            "data": {"symbol": symbol, "items": items},
        }
