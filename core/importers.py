"""CSV import for portfolio transactions.

Two layouts are recognised from the header row:

* Zerodha Console tradebook: symbol, trade_date, exchange, segment, trade_type, quantity, price ...
  (equity rows only; NSE -> ``.NS``, BSE -> ``.BO``; F&O / currency / commodity rows skipped).
* Generic: date, symbol, side (buy/sell), quantity, price, [fees], [exchange], [note]
  Header names are matched loosely (``qty``, ``type``, ``trade_type``, ``ticker``, ``charges`` ...).

Each row becomes a validated ``TransactionIn`` or a per-line error; one bad row never
blocks the rest.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Any

from pydantic import BaseModel, ValidationError

from core.portfolio import TransactionIn

_ALIASES = {
    "date": ("date", "trade_date", "tradedate", "trade date", "order_execution_time", "execution date"),
    "symbol": ("symbol", "ticker", "tradingsymbol", "scrip", "stock"),
    "side": ("side", "type", "trade_type", "transaction type", "action", "buy/sell"),
    "quantity": ("quantity", "qty", "shares", "units"),
    "price": ("price", "rate", "trade price", "avg price", "average price"),
    "fees": ("fees", "charges", "brokerage", "commission"),
    "exchange": ("exchange", "exch"),
    "segment": ("segment",),
    "note": ("note", "notes", "remarks"),
}
_EXCHANGE_SUFFIX = {"NSE": ".NS", "BSE": ".BO"}
_EQUITY_SEGMENTS = {"EQ", "EQUITY", "CASH", ""}


class ImportError_(BaseModel):
    line: int
    message: str


class ImportResult(BaseModel):
    format: str
    rows: list[TransactionIn]
    errors: list[ImportError_]
    skipped: int = 0


def _parse_date(value: str) -> dt.date:
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%b-%Y", "%d %b %Y", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(value[:19] if "T" in value or ":" in value else value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date {value!r} (use YYYY-MM-DD or DD-MM-YYYY)")


def _number(value: str) -> float:
    cleaned = value.replace(",", "").replace("₹", "").replace("$", "").strip()
    if cleaned == "":
        raise ValueError("empty number")
    return float(cleaned)


def _side(value: str) -> str:
    v = value.strip().lower()
    if v in ("buy", "b", "purchase", "bought"):
        return "buy"
    if v in ("sell", "s", "sale", "sold"):
        return "sell"
    raise ValueError(f"side must be buy or sell, got {value!r}")


def _resolve(headers: list[str]) -> dict[str, str]:
    lowered = {h.strip().lower(): h for h in headers if h}
    out = {}
    for field, names in _ALIASES.items():
        hit = next((lowered[n] for n in names if n in lowered), None)
        if hit:
            out[field] = hit
    return out


def parse_csv(text: str) -> ImportResult:
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    cols = _resolve(headers)
    lowered = {h.strip().lower() for h in headers if h}
    fmt = "zerodha" if {"trade_type", "trade_date", "exchange"} <= lowered else "generic"
    missing = [f for f in ("date", "symbol", "side", "quantity", "price") if f not in cols]
    if missing:
        return ImportResult(format=fmt, rows=[], errors=[ImportError_(
            line=1, message=f"missing column(s): {', '.join(missing)}. Expected date, symbol, side, quantity, "
                            f"price [, fees, exchange]; found: {', '.join(headers) or 'nothing'}")])

    rows: list[TransactionIn] = []
    errors: list[ImportError_] = []
    skipped = 0
    for line_no, raw in enumerate(reader, start=2):
        get = lambda f: (raw.get(cols[f]) or "").strip() if f in cols else ""  # noqa: E731
        if not any((v or "").strip() for v in raw.values() if isinstance(v, str)):
            continue
        segment = get("segment").upper()
        if segment and segment not in _EQUITY_SEGMENTS:
            skipped += 1
            continue
        try:
            symbol = get("symbol").upper()
            exchange = get("exchange").upper()
            if exchange in _EXCHANGE_SUFFIX and "." not in symbol:
                symbol += _EXCHANGE_SUFFIX[exchange]
            data: dict[str, Any] = {
                "symbol": symbol, "side": _side(get("side")), "quantity": _number(get("quantity")),
                "price": _number(get("price")), "trade_date": _parse_date(get("date")),
                "fees": _number(get("fees")) if get("fees") else 0.0, "note": get("note")[:200],
            }
            rows.append(TransactionIn(**data))
        except ValidationError as exc:
            errors.append(ImportError_(line=line_no, message="; ".join(
                f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:3])))
        except ValueError as exc:
            errors.append(ImportError_(line=line_no, message=str(exc)))
    return ImportResult(format=fmt, rows=rows, errors=errors, skipped=skipped)
