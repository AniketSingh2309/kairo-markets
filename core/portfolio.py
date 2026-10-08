"""Portfolio book-keeping: FIFO lots, realized/unrealized P&L, XIRR.

FIFO (first-in, first-out) is the lot-matching method Indian tax law applies to
shares, so realized gains here line up with ``core.tax_india``. Fees on a buy are
added to that lot's cost; fees on a sell reduce its proceeds.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from core.finance import xirr
from tools.models import Symbol

Side = Literal["buy", "sell"]


class TransactionIn(BaseModel):
    symbol: Symbol
    side: Side
    quantity: float = Field(gt=0)
    price: float = Field(gt=0, description="Per-unit price in the instrument's quote currency")
    fees: float = Field(0.0, ge=0, description="Brokerage + taxes + charges for the whole trade")
    trade_date: dt.date
    note: str = Field("", max_length=200)

    @field_validator("trade_date")
    @classmethod
    def _not_future(cls, value: dt.date) -> dt.date:
        if value > dt.date.today() + dt.timedelta(days=1):
            raise ValueError("trade_date is in the future")
        if value.year < 1990:
            raise ValueError("trade_date must be 1990 or later")
        return value

    @field_validator("symbol")
    @classmethod
    def _tradable(cls, value: str) -> str:
        if value.startswith("^") or value.endswith(("=X", "=F")):
            raise ValueError("indices, FX pairs and futures cannot be held in the portfolio")
        return value


class Transaction(TransactionIn):
    id: int
    currency: str | None = None
    source: str = "manual"


class OpenLot(BaseModel):
    txn_id: int
    date: dt.date
    quantity: float
    unit_cost: float  # price + allocated buy fees


class RealizedLot(BaseModel):
    symbol: str
    currency: str | None
    quantity: float
    buy_date: dt.date
    sell_date: dt.date
    unit_cost: float
    unit_proceeds: float
    cost: float
    proceeds: float
    gain: float
    holding_days: int


class BookIssue(BaseModel):
    txn_id: int
    symbol: str
    message: str


@dataclass
class _SymbolBook:
    symbol: str
    currency: str | None = None
    lots: list[OpenLot] = field(default_factory=list)
    flows: list[tuple[dt.date, float]] = field(default_factory=list)


@dataclass
class Book:
    symbols: dict[str, _SymbolBook]
    realized: list[RealizedLot]
    issues: list[BookIssue]

    def open_symbols(self) -> list[str]:
        return [s for s, b in self.symbols.items() if sum(l.quantity for l in b.lots) > 1e-9]


_EPS = 1e-9


def build_book(transactions: list[Transaction]) -> Book:
    """Replay transactions in date order (buys before sells on the same day) with FIFO matching."""
    books: dict[str, _SymbolBook] = {}
    realized: list[RealizedLot] = []
    issues: list[BookIssue] = []
    ordered = sorted(transactions, key=lambda t: (t.trade_date, 0 if t.side == "buy" else 1, t.id))
    for t in ordered:
        b = books.setdefault(t.symbol, _SymbolBook(t.symbol))
        b.currency = b.currency or t.currency
        if t.side == "buy":
            b.lots.append(OpenLot(txn_id=t.id, date=t.trade_date, quantity=t.quantity,
                                  unit_cost=(t.quantity * t.price + t.fees) / t.quantity))
            b.flows.append((t.trade_date, -(t.quantity * t.price + t.fees)))
            continue

        held = sum(l.quantity for l in b.lots)
        qty = t.quantity
        if qty > held + _EPS:
            issues.append(BookIssue(txn_id=t.id, symbol=t.symbol, message=(
                f"sell of {qty:g} on {t.trade_date} exceeds the {held:g} held; only {held:g} matched "
                "(missing earlier buys or an unrecorded split/bonus?)")))
            qty = held
        if qty <= _EPS:
            continue
        unit_proceeds = (t.price * t.quantity - t.fees) / t.quantity
        b.flows.append((t.trade_date, unit_proceeds * qty))
        remaining = qty
        while remaining > _EPS and b.lots:
            lot = b.lots[0]
            take = min(lot.quantity, remaining)
            realized.append(RealizedLot(
                symbol=t.symbol, currency=b.currency, quantity=take, buy_date=lot.date, sell_date=t.trade_date,
                unit_cost=lot.unit_cost, unit_proceeds=unit_proceeds, cost=take * lot.unit_cost,
                proceeds=take * unit_proceeds, gain=take * (unit_proceeds - lot.unit_cost),
                holding_days=(t.trade_date - lot.date).days))
            lot.quantity -= take
            remaining -= take
            if lot.quantity <= _EPS:
                b.lots.pop(0)
    return Book(symbols=books, realized=realized, issues=issues)


# ---------------------------------------------------------------------------
# Valuation
# ---------------------------------------------------------------------------


class QuoteLite(BaseModel):
    price: float
    previous_close: float | None = None
    currency: str | None = None
    name: str | None = None
    market_time: dt.datetime | None = None


class PositionView(BaseModel):
    symbol: str
    name: str | None
    currency: str | None
    quantity: float
    avg_cost: float
    invested: float
    price: float | None
    previous_close: float | None
    value: float | None
    pnl: float | None
    pnl_pct: float | None
    day_change: float | None
    day_change_pct: float | None
    weight: float | None = None
    realized_pnl: float = 0.0
    xirr: float | None = None
    first_buy: dt.date
    lots: list[OpenLot]
    quote_time: dt.datetime | None = None


class CurrencyGroup(BaseModel):
    currency: str
    positions: int
    invested: float
    value: float
    pnl: float
    pnl_pct: float | None
    day_change: float
    realized_pnl: float
    xirr: float | None
    value_in_base: float | None = None


class PortfolioView(BaseModel):
    as_of: dt.datetime
    base_currency: str
    groups: list[CurrencyGroup]
    total_value_in_base: float | None
    total_invested_in_base: float | None
    fx_rates: dict[str, float]
    positions: list[PositionView]
    closed: list[dict[str, Any]]
    issues: list[BookIssue]
    missing_quotes: list[str]


def value_portfolio(book: Book, quotes: dict[str, QuoteLite], *, today: dt.date,
                    base_currency: str = "INR", fx_to_base: dict[str, float] | None = None) -> PortfolioView:
    fx_to_base = {base_currency: 1.0, **(fx_to_base or {})}
    realized_by_symbol: dict[str, float] = defaultdict(float)
    for r in book.realized:
        realized_by_symbol[r.symbol] += r.gain

    positions: list[PositionView] = []
    closed: list[dict[str, Any]] = []
    missing: list[str] = []
    group_flows: dict[str, list[tuple[dt.date, float]]] = defaultdict(list)
    currency_of: dict[str, str] = {}
    for sym, b in book.symbols.items():
        qty = sum(l.quantity for l in b.lots)
        q = quotes.get(sym)
        currency = currency_of[sym] = (q.currency if q and q.currency else None) or b.currency or "?"
        group_flows[currency] += b.flows
        if qty <= _EPS:
            closed.append({"symbol": sym, "currency": currency, "realized_pnl": realized_by_symbol[sym]})
            continue
        invested = sum(l.quantity * l.unit_cost for l in b.lots)
        price = q.price if q else None
        if q is None:
            missing.append(sym)
        value = qty * price if price is not None else None
        pnl = value - invested if value is not None else None
        prev = q.previous_close if q else None
        day = qty * (price - prev) if price is not None and prev else None
        flows = list(b.flows) + ([(today, value)] if value is not None else [])
        if value is not None:
            group_flows[currency].append((today, value))
        positions.append(PositionView(
            symbol=sym, name=q.name if q else None, currency=currency, quantity=qty, avg_cost=invested / qty,
            invested=invested, price=price, previous_close=prev, value=value, pnl=pnl,
            pnl_pct=(pnl / invested * 100) if pnl is not None and invested else None,
            day_change=day, day_change_pct=((price - prev) / prev * 100) if day is not None else None,
            realized_pnl=realized_by_symbol[sym], xirr=xirr(flows) if value is not None else None,
            first_buy=min(l.date for l in b.lots), lots=b.lots, quote_time=q.market_time if q else None))

    groups: list[CurrencyGroup] = []
    for cur in sorted({p.currency for p in positions} | {c["currency"] for c in closed}):
        ps = [p for p in positions if p.currency == cur]
        value = sum(p.value or 0 for p in ps)
        for p in ps:
            p.weight = (p.value / value * 100) if value and p.value is not None else None
        invested = sum(p.invested for p in ps)
        realized = sum(r.gain for r in book.realized if currency_of.get(r.symbol) == cur)
        fx = fx_to_base.get(cur)
        groups.append(CurrencyGroup(
            currency=cur, positions=len(ps), invested=invested, value=value, pnl=value - invested,
            pnl_pct=((value - invested) / invested * 100) if invested else None,
            day_change=sum(p.day_change or 0 for p in ps), realized_pnl=realized,
            xirr=xirr(group_flows[cur]), value_in_base=value * fx if fx is not None else None))

    convertible = all(g.value_in_base is not None for g in groups)
    return PortfolioView(
        as_of=dt.datetime.now(dt.timezone.utc), base_currency=base_currency, groups=groups,
        total_value_in_base=sum(g.value_in_base or 0 for g in groups) if convertible else None,
        total_invested_in_base=sum(g.invested * fx_to_base[g.currency] for g in groups) if convertible else None,
        fx_rates=fx_to_base, positions=sorted(positions, key=lambda p: -(p.value or 0)),
        closed=closed, issues=book.issues, missing_quotes=missing)
