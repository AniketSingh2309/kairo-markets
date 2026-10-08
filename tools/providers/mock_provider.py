"""Mock market-data provider backed by a static JSON file.

The dataset (``tools/data/mock_market_data.json``) is fictional. It contains a
few ordinary symbols plus deliberately broken ones so every failure path of the
tool contract can be exercised end to end:

* ``MSFT``  - announcements feed lags the price feed by ~17 days (timestamp skew)
* ``TSLA``  - price feed stopped a week before the announcements feed (stale prices)
* ``NVDA``  - quote feed and history feed disagree on the same day's close
* ``BADF``  - price rows are malformed (``"N/A"`` close, high < low)
* ``SLOW``  - every call sleeps longer than the default tool timeout
* ``RWLK``  - 520 bars of a seeded pure random walk (forecast must admit it has no edge)
* anything else - unknown symbol

Rows in ``bars`` are ``[date, open, high, low, close, volume]``.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path
from typing import Any, Callable

from tools.providers.base import NoDataAvailable, ProviderUnavailable, SymbolNotFound

DEFAULT_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "mock_market_data.json"
_BAR_KEYS = ("date", "open", "high", "low", "close", "volume")


def _row_to_dict(row: Any) -> Any:
    # Deliberately lenient: a malformed row is passed through for the tool layer to reject.
    if isinstance(row, (list, tuple)):
        return dict(zip(_BAR_KEYS, row))
    return row


def _row_date(row: Any) -> str | None:
    if isinstance(row, (list, tuple)) and row:
        return str(row[0])
    if isinstance(row, dict):
        return str(row.get("date"))
    return None


def _parse_day(value: str | None) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _parse_ts(value: Any) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class MockMarketDataProvider:
    name = "mock"

    def __init__(
        self,
        data_path: str | Path | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ):
        path = Path(data_path) if data_path else DEFAULT_DATA_PATH
        with path.open(encoding="utf-8") as fh:
            self._data: dict[str, Any] = json.load(fh)
        self._sleep = sleep
        self.version: str = self._data.get("version", "v1")

    @property
    def symbols(self) -> list[str]:
        return sorted(self._data["symbols"])

    def raw_record(self, symbol: str) -> dict[str, Any]:
        """Direct access to the underlying record (used by the eval as ground truth)."""
        return self._data["symbols"][symbol]

    # -- internals -----------------------------------------------------------

    def _record(self, symbol: str, endpoint: str) -> dict[str, Any]:
        record = self._data["symbols"].get(symbol)
        if record is None:
            raise SymbolNotFound(f"symbol {symbol!r} is not in the mock dataset")
        simulate = record.get("simulate") or {}
        latency = simulate.get("latency_seconds")
        if latency:
            self._sleep(float(latency))
        if endpoint in simulate.get("unavailable_endpoints", []):
            raise ProviderUnavailable(f"mock {endpoint} endpoint is down for {symbol}")
        return record

    def _source_id(self, endpoint: str, symbol: str, **params: Any) -> str:
        query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        base = f"mock://market-data/{self.version}/{endpoint}/{symbol}"
        return f"{base}?{query}" if query else base

    @staticmethod
    def _close_time(record: dict[str, Any], day: Any) -> str:
        return f"{day}T{record.get('market_close_utc', '20:00:00')}Z"

    # -- endpoints -----------------------------------------------------------

    def fetch_quote(self, symbol: str, on_date: dt.date | None) -> dict[str, Any]:
        record = self._record(symbol, "quote")
        bars = record.get("bars") or []
        if on_date is None:
            bar = record.get("quote_override") or (_row_to_dict(bars[-1]) if bars else None)
            if bar is None:
                raise NoDataAvailable(f"no price data for {symbol}")
        else:
            bar = next(
                (_row_to_dict(r) for r in bars if _row_date(r) == on_date.isoformat()), None
            )
            if bar is None:
                raise NoDataAvailable(f"no trading bar for {symbol} on {on_date.isoformat()}")
        day = bar.get("date") if isinstance(bar, dict) else None
        return {
            "meta": {
                "source_id": self._source_id(
                    "quote", symbol, date=on_date.isoformat() if on_date else None
                ),
                "url": None,
                "observed_at": self._close_time(record, day),
            },
            "data": {"symbol": symbol, "currency": record.get("currency"), "bar": bar},
        }

    def fetch_daily_snapshot(self, symbol: str) -> dict[str, Any]:
        record = self._record(symbol, "snapshot")
        rows = [_row_to_dict(r) for r in (record.get("bars") or [])][-22:]
        if not rows:
            raise NoDataAvailable(f"no price data for {symbol}")
        last, prev = rows[-1], (rows[-2] if len(rows) > 1 else {})
        vols = [r.get("volume") for r in rows[:-1][-20:] if isinstance(r.get("volume"), int)]
        observed = self._close_time(record, last.get("date"))
        year = [_row_to_dict(r) for r in (record.get("bars") or [])][-252:]
        return {
            "meta": {"source_id": self._source_id("snapshot", symbol), "url": None, "observed_at": observed},
            "data": {"symbol": symbol, "name": record.get("name"), "exchange": record.get("exchange"),
                     "currency": record.get("currency"), "price": last.get("close"),
                     "previous_close": prev.get("close"), "volume": last.get("volume"),
                     "avg_volume": sum(vols) / len(vols) if vols else None,
                     "fifty_two_week_high": max((r.get("high") for r in year if isinstance(r.get("high"), (int, float))), default=None),
                     "fifty_two_week_low": min((r.get("low") for r in year if isinstance(r.get("low"), (int, float))), default=None),
                     "session_date": last.get("date"), "market_time": observed,
                     "closes": [r.get("close") for r in rows]},
        }

    def fetch_profile(self, symbol: str) -> dict[str, Any]:
        record = self._record(symbol, "profile")
        return {
            "meta": {"source_id": self._source_id("profile", symbol), "url": None,
                     "observed_at": self._close_time(record, _row_date((record.get("bars") or [[None]])[-1]))},
            "data": {"symbol": symbol, "name": record.get("name"), "sector": record.get("sector"),
                     "industry": record.get("industry"), "quote_type": "EQUITY",
                     "exchange": record.get("exchange")},
        }

    def fetch_fundamentals(self, symbol: str, statements: bool = True) -> dict[str, Any]:
        record = self._record(symbol, "profile")
        f = record.get("fundamentals") or {}
        return {"meta": {"source_id": self._source_id("fundamentals", symbol), "url": None,
                         "observed_at": self._close_time(record, _row_date((record.get("bars") or [[None]])[-1]))},
                "data": {"symbol": symbol, "name": record.get("name"), "currency": record.get("currency"),
                         "sector": record.get("sector"), "industry": record.get("industry"), **f,
                         "annual": f.get("annual", []) if statements else [], "quarterly": f.get("quarterly", []) if statements else []}}

    def fetch_company(self, symbol: str) -> dict[str, Any]:
        record = self._record(symbol, "profile")
        return {
            "meta": {"source_id": self._source_id("company", symbol), "url": None,
                     "observed_at": self._close_time(record, _row_date((record.get("bars") or [[None]])[-1]))},
            "data": {"symbol": symbol, "name": record.get("name"), "description": record.get("description"),
                     "sector": record.get("sector"), "industry": record.get("industry"), "quote_type": "EQUITY"},
        }

    def fetch_live(self, symbol: str) -> dict[str, Any]:
        """Mock data has no intraday feed: report the last bar as a closed market."""
        record = self._record(symbol, "live")
        bars = record.get("bars") or []
        if not bars:
            raise NoDataAvailable(f"no price data for {symbol}")
        bar = _row_to_dict(bars[-1])
        prev = _row_to_dict(bars[-2]) if len(bars) > 1 else {}
        observed = self._close_time(record, bar.get("date"))
        return {
            "meta": {"source_id": self._source_id("live", symbol), "url": None, "observed_at": observed},
            "data": {"symbol": symbol, "currency": record.get("currency"),
                     "name": record.get("name"), "exchange": record.get("exchange"),
                     "open": bar.get("open"), "volume": bar.get("volume"),
                     "price": bar.get("close"), "market_time": observed,
                     "previous_close": prev.get("close"),
                     "day_high": bar.get("high"), "day_low": bar.get("low"),
                     "market_state": "closed", "points": [{"t": observed, "price": bar.get("close")}]},
        }

    def fetch_price_history(self, symbol: str, lookback_days: int) -> dict[str, Any]:
        record = self._record(symbol, "history")
        bars = record.get("bars") or []
        if not bars:
            raise NoDataAvailable(f"no price history for {symbol}")
        last_day = _parse_day(_row_date(bars[-1]))
        cutoff = last_day - dt.timedelta(days=lookback_days) if last_day else None
        window = [
            _row_to_dict(r)
            for r in bars
            if cutoff is None or (_parse_day(_row_date(r)) or cutoff) >= cutoff
        ]
        return {
            "meta": {
                "source_id": self._source_id("history", symbol, lookback=f"{lookback_days}d"),
                "url": None,
                "observed_at": self._close_time(record, _row_date(bars[-1])),
            },
            "data": {"symbol": symbol, "currency": record.get("currency"), "name": record.get("name"),
                     "bars": window},
        }

    CHART_DAYS = {"3M": 92, "6M": 183, "1Y": 366, "5Y": 5 * 366, "ALL": None}

    def fetch_bars(self, symbol: str, interval: str) -> dict[str, Any]:
        if interval != "1D":
            raise NoDataAvailable(f"no intraday bars for {symbol} in the mock dataset")
        chart = self.fetch_chart(symbol, "ALL")
        data = chart["data"]
        return {"meta": chart["meta"], "data": {"symbol": symbol, "name": data["name"], "currency": data["currency"],
                                                "interval": "1D", "previous_close": None, "bars": data["bars"]}}

    def fetch_chart(self, symbol: str, chart_range: str) -> dict[str, Any]:
        """Daily ranges only: the fictional dataset has no intraday bars."""
        record = self._record(symbol, "history")
        if chart_range not in self.CHART_DAYS:
            raise NoDataAvailable(f"no intraday bars for {symbol} in the mock dataset")
        rows = [_row_to_dict(r) for r in record.get("bars") or []]
        if not rows:
            raise NoDataAvailable(f"no price history for {symbol}")
        days = [_parse_day(r.get("date")) for r in rows]
        span = self.CHART_DAYS[chart_range]
        visible = days[0] if span is None else days[-1] - dt.timedelta(days=span)
        bars = [{"t": dt.datetime.combine(d, dt.time(), tzinfo=dt.timezone.utc).isoformat(),
                 **{k: r.get(k) for k in ("open", "high", "low", "close", "volume")}} for d, r in zip(days, rows)]
        before = [r for d, r in zip(days, rows) if d < visible]
        return {
            "meta": {"source_id": self._source_id("chart", symbol, range=chart_range), "url": None,
                     "observed_at": self._close_time(record, _row_date(record["bars"][-1]))},
            "data": {"symbol": symbol, "name": record.get("name"), "currency": record.get("currency"),
                     "range": chart_range, "interval": "1d", "intraday": False,
                     "previous_close": before[-1]["close"] if before else None,
                     "visible_from": dt.datetime.combine(visible, dt.time(), tzinfo=dt.timezone.utc).isoformat(),
                     "bars": bars},
        }

    def fetch_announcements(self, symbol: str, lookback_days: int) -> dict[str, Any]:
        record = self._record(symbol, "announcements")
        feed = record.get("announcements")
        if feed is None:
            raise NoDataAvailable(f"no announcements feed for {symbol}")
        as_of = _parse_ts(feed.get("as_of"))
        items = list(feed.get("items", []))
        if as_of is not None:
            cutoff = as_of - dt.timedelta(days=lookback_days)
            items = [i for i in items if (_parse_ts(i.get("published_at")) or as_of) >= cutoff]
        return {
            "meta": {
                "source_id": self._source_id(
                    "announcements", symbol, lookback=f"{lookback_days}d"
                ),
                "url": None,
                "observed_at": feed.get("as_of"),
            },
            "data": {"symbol": symbol, "items": items},
        }
