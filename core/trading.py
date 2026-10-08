"""Paper trading engine: order rules, matching and the account book (pure functions, no I/O).

Everything is derived from the list of fills, so the book can never drift from the history:

* Cash products (delivery stocks, and all options) move cash by the full trade value.
* Intraday stocks (MIS) move cash only by realised P&L, and block 20% margin (5x) while open.
* Positions are today's activity per (symbol, product); holdings are cash-product quantity carried
  in from earlier sessions.

What's simulated: market, limit, stop-loss (SL) and stop-loss-market (SL-M) orders, fills at the
live price, the 20% intraday margin, option writing with a rough 15%-of-strike margin per short,
GTT / OCO triggers, intraday square-off at 15:20 IST, and DAY validity. What isn't: brokerage and
taxes, exchange holidays, partial fills, SPAN / hedge margin benefit, and expiry settlement.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from tools.models import normalize_symbol

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SESSION_OPEN, SESSION_CLOSE, SQUARE_OFF = dt.time(9, 15), dt.time(15, 30), dt.time(15, 20)
INTRADAY_MARGIN = 0.20
SHORT_OPTION_MARGIN = 0.15  # of strike x quantity, per short option (a rough stand-in for SPAN + exposure)
DEFAULT_CASH = 1_000_000.0
OPTION_RE = re.compile(r"^OPT:OPT(IDX|STK)[A-Z0-9&\-]+?\d{2}-\d{2}-\d{4}(CE|PE)\d+(?:\.\d+)?$")

Side = Literal["BUY", "SELL"]
OrderType = Literal["MARKET", "LIMIT", "SL", "SL-M"]
Product = Literal["INTRADAY", "DELIVERY"]


def is_option(symbol: str) -> bool:
    return symbol.startswith("OPT:")


def option_strike(symbol: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)$", symbol)
    return float(m.group(1)) if m else 0.0


def cash_product(symbol: str, product: str) -> bool:
    """Options and delivery stocks pay the full value; intraday stocks trade on margin."""
    return is_option(symbol) or product == "DELIVERY"


def ist(at: dt.datetime) -> dt.datetime:
    return at.astimezone(IST)


def is_trading_day(day: dt.date) -> bool:
    return day.weekday() < 5  # exchange holidays aren't modelled


def market_open(now: dt.datetime) -> bool:
    t = ist(now)
    return is_trading_day(t.date()) and SESSION_OPEN <= t.time() < SESSION_CLOSE


def order_session(placed: dt.datetime) -> dt.date:
    """The session a DAY order belongs to: today if placed before the close on a trading day, else the next one."""
    t = ist(placed)
    day = t.date()
    if not is_trading_day(day) or t.time() >= SESSION_CLOSE:
        day += dt.timedelta(days=1)
        while not is_trading_day(day):
            day += dt.timedelta(days=1)
    return day


# ------------------------------------------------------------------------------------------ orders


class OrderIn(BaseModel):
    symbol: str = Field(max_length=64)
    side: Side
    qty: int = Field(gt=0, le=1_000_000)
    order_type: OrderType = "MARKET"
    price: float | None = Field(None, gt=0)
    trigger_price: float | None = Field(None, gt=0)
    product: Product = "DELIVERY"

    @field_validator("symbol")
    @classmethod
    def _tradable(cls, v: str) -> str:
        v = v.strip().upper()
        if v.startswith("OPT:"):
            if not OPTION_RE.match(v):
                raise ValueError("not an NSE option contract")
            return v
        sym = normalize_symbol(v)
        if not sym.endswith((".NS", ".BO")):
            raise ValueError("only NSE / BSE stocks and NSE options can be traded")
        return sym

    @model_validator(mode="after")
    def _prices(self) -> OrderIn:
        if self.order_type in ("LIMIT", "SL") and self.price is None:
            raise ValueError(f"a {self.order_type} order needs a price")
        if self.order_type in ("SL", "SL-M") and self.trigger_price is None:
            raise ValueError(f"a {self.order_type} order needs a trigger price")
        if self.order_type == "SL" and self.price is not None and self.trigger_price is not None:
            if self.side == "BUY" and self.price < self.trigger_price:
                raise ValueError("for a stop-loss buy, the limit price must be at or above the trigger")
            if self.side == "SELL" and self.price > self.trigger_price:
                raise ValueError("for a stop-loss sell, the limit price must be at or below the trigger")
        if self.order_type in ("MARKET", "SL-M"):
            self.price = None
        if self.order_type in ("MARKET", "LIMIT"):
            self.trigger_price = None
        return self


def match(order: dict, ltp: float) -> tuple[str, float | None]:
    """(new status, fill price or None) for an open order at the latest price."""
    side, kind, status = order["side"], order["order_type"], order["status"]
    trig, limit = order.get("trigger_price"), order.get("price")
    if kind in ("SL", "SL-M") and status == "open":
        hit = ltp >= trig if side == "BUY" else ltp <= trig
        if not hit:
            return "open", None
        if kind == "SL-M":
            return "complete", ltp
        status = "triggered"  # becomes a limit order at `price`
    if kind == "MARKET":
        return "complete", ltp
    if side == "BUY" and ltp <= limit:
        return "complete", ltp
    if side == "SELL" and ltp >= limit:
        return "complete", ltp
    return status, None


# ------------------------------------------------------------------------------------------ book


@dataclass
class Line:
    """Net position for one (symbol, product)."""

    symbol: str
    product: str
    qty: float = 0.0
    avg: float = 0.0
    realised: float = 0.0          # all-time, for this line
    realised_today: float = 0.0
    day_buy_qty: float = 0.0
    day_sell_qty: float = 0.0
    day_buy_value: float = 0.0
    day_sell_value: float = 0.0
    qty_at_open: float = 0.0       # quantity carried into today (holdings)

    def apply(self, side: str, qty: float, price: float, today: bool) -> float:
        """Apply a fill; returns the realised P&L it produced."""
        signed = qty if side == "BUY" else -qty
        realised = 0.0
        if self.qty == 0 or (self.qty > 0) == (signed > 0):          # opening / adding
            total = abs(self.qty) + qty
            self.avg = (abs(self.qty) * self.avg + qty * price) / total
            self.qty += signed
        else:                                                         # reducing / closing / flipping
            closing = min(qty, abs(self.qty))
            realised = (price - self.avg) * closing * (1 if self.qty > 0 else -1)
            self.qty += signed
            if abs(self.qty) < 1e-9:
                self.qty, self.avg = 0.0, 0.0
            elif (self.qty > 0) == (signed > 0):                      # flipped: remainder opens at this price
                self.avg = price
        self.realised += realised
        if today:
            self.realised_today += realised
            if side == "BUY":
                self.day_buy_qty += qty; self.day_buy_value += qty * price
            else:
                self.day_sell_qty += qty; self.day_sell_value += qty * price
        return realised


@dataclass
class Book:
    start_cash: float
    cash: float
    lines: dict[tuple[str, str], Line] = field(default_factory=dict)

    def line(self, symbol: str, product: str) -> Line:
        return self.lines.setdefault((symbol, product), Line(symbol, product))

    def margin_used(self) -> float:
        intraday = sum(abs(ln.qty) * ln.avg * INTRADAY_MARGIN for ln in self.lines.values()
                       if ln.qty and not cash_product(ln.symbol, ln.product))
        shorts = sum(-ln.qty * option_strike(ln.symbol) * SHORT_OPTION_MARGIN for ln in self.lines.values()
                     if is_option(ln.symbol) and ln.qty < 0)
        return intraday + shorts

    def cash_qty(self, symbol: str) -> float:
        """Long quantity held in cash products (delivery + options), across products."""
        return sum(ln.qty for (s, p), ln in self.lines.items() if s == symbol and cash_product(s, p))


def replay(fills: list[dict], start_cash: float, today: dt.date) -> Book:
    """Rebuild the account from its fills (each: at, symbol, side, qty, price, product)."""
    book = Book(start_cash=start_cash, cash=start_cash)
    carried = False
    for f in sorted(fills, key=lambda f: (f["at"], f.get("id", 0))):
        day = ist(f["at"]).date()
        if day >= today and not carried:
            for ln in book.lines.values():
                ln.qty_at_open = ln.qty
            carried = True
        ln = book.line(f["symbol"], f["product"])
        realised = ln.apply(f["side"], f["qty"], f["price"], today=day >= today)
        if cash_product(f["symbol"], f["product"]):
            book.cash += -f["qty"] * f["price"] if f["side"] == "BUY" else f["qty"] * f["price"]
        else:
            book.cash += realised
    if not carried:
        for ln in book.lines.values():
            ln.qty_at_open = ln.qty
    return book


def reserve_for(order: dict, ltp: float | None, book: Book) -> float:
    """Funds an open order would need if it filled now."""
    px = order.get("price") or order.get("trigger_price") or ltp or 0.0
    value = order["qty"] * px
    if is_option(order["symbol"]):
        held = book.cash_qty(order["symbol"])
        if order["side"] == "BUY":
            covering = min(order["qty"], max(0.0, -held))      # buying back a short frees margin rather than using it
            return (order["qty"] - covering) * px
        writing = max(0.0, order["qty"] - max(0.0, held))      # selling more than you hold opens a short
        return writing * option_strike(order["symbol"]) * SHORT_OPTION_MARGIN
    if cash_product(order["symbol"], order["product"]):
        return value if order["side"] == "BUY" else 0.0
    ln = book.lines.get((order["symbol"], order["product"]))
    reduces = ln is not None and ln.qty and ((ln.qty > 0) != (order["side"] == "BUY"))
    return 0.0 if reduces else value * INTRADAY_MARGIN


def funds(book: Book, open_orders: list[dict], ltps: dict[str, float]) -> dict[str, float]:
    reserved = sum(reserve_for(o, ltps.get(o["symbol"]), book) for o in open_orders)
    used = book.margin_used()
    return {"start_cash": round(book.start_cash, 2), "cash": round(book.cash, 2), "margin_used": round(used, 2),
            "reserved": round(reserved, 2), "available": round(book.cash - used - reserved, 2)}


def check_order(o: OrderIn, ltp: float | None, book: Book, open_orders: list[dict], ltps: dict[str, float]) -> str | None:
    """Why the order can't be placed, or None if it can."""
    if ltp is None and o.order_type in ("MARKET", "SL-M"):
        return "No live price for this instrument right now."
    if not is_option(o.symbol) and o.product == "DELIVERY" and o.side == "SELL":
        pending = sum(x["qty"] for x in open_orders if x["symbol"] == o.symbol and x["side"] == "SELL" and x["product"] == "DELIVERY")
        held = book.cash_qty(o.symbol)
        if held - pending < o.qty:
            return f"You hold {max(0, held - pending):g} to sell. Short selling is intraday only — choose Intraday."
    need = reserve_for({**o.model_dump(), "status": "open"}, ltp, book)
    have = funds(book, open_orders, ltps)["available"]
    if need > have + 1e-6:
        return f"Not enough funds: this order needs ₹{need:,.2f}, available ₹{have:,.2f}."
    return None


def position_rows(book: Book, ltps: dict[str, float]) -> list[dict]:
    """Today's positions: anything traded today, open intraday trades, and every open option (as brokers show F&O)."""
    out = []
    for ln in book.lines.values():
        if not (ln.day_buy_qty or ln.day_sell_qty or (ln.qty and (not cash_product(ln.symbol, ln.product) or is_option(ln.symbol)))):
            continue
        ltp = ltps.get(ln.symbol)
        today_qty = ln.qty - (ln.qty_at_open if cash_product(ln.symbol, ln.product) and not is_option(ln.symbol) else 0)
        unreal = (ltp - ln.avg) * ln.qty if ltp is not None and ln.qty else 0.0
        out.append({"symbol": ln.symbol, "product": ln.product, "qty": round(ln.qty, 4), "today_qty": round(today_qty, 4),
                    "avg": round(ln.avg, 4), "ltp": ltp, "realised": round(ln.realised_today, 2), "unrealised": round(unreal, 2),
                    "pnl": round(ln.realised_today + unreal, 2),
                    "buy_qty": ln.day_buy_qty, "sell_qty": ln.day_sell_qty,
                    "buy_avg": round(ln.day_buy_value / ln.day_buy_qty, 4) if ln.day_buy_qty else None,
                    "sell_avg": round(ln.day_sell_value / ln.day_sell_qty, 4) if ln.day_sell_qty else None})
    return sorted(out, key=lambda r: (r["qty"] == 0, r["symbol"]))


def holding_rows(book: Book, ltps: dict[str, float], prev_closes: dict[str, float]) -> list[dict]:
    """Cash-product quantity carried in from earlier sessions."""
    agg: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for ln in book.lines.values():
        if cash_product(ln.symbol, ln.product) and not is_option(ln.symbol) and ln.qty > 0 and ln.qty_at_open > 0:
            qty = min(ln.qty, ln.qty_at_open)
            a = agg[ln.symbol]
            a[1] = (a[0] * a[1] + qty * ln.avg) / (a[0] + qty)
            a[0] += qty
    out = []
    for sym, (qty, avg) in agg.items():
        ltp, prev = ltps.get(sym), prev_closes.get(sym)
        out.append({"symbol": sym, "qty": round(qty, 4), "avg": round(avg, 4), "ltp": ltp, "invested": round(qty * avg, 2),
                    "value": round(qty * ltp, 2) if ltp is not None else None,
                    "pnl": round((ltp - avg) * qty, 2) if ltp is not None else None,
                    "day_change": round((ltp - prev) * qty, 2) if ltp is not None and prev else None})
    return sorted(out, key=lambda r: r["symbol"])


def to_square_off(book: Book) -> list[Line]:
    """Intraday lines still open (closed at 15:20 IST)."""
    return [ln for ln in book.lines.values() if ln.product == "INTRADAY" and abs(ln.qty) > 1e-9]


# ------------------------------------------------------------------------------------------ GTT / OCO


class GttIn(BaseModel):
    """Good-till-triggered order. ``single``: one trigger places one order. ``oco``: a target and a stop-loss
    on a position; whichever triggers first places its order and cancels the other."""

    symbol: str = Field(max_length=64)
    kind: Literal["single", "oco"] = "single"
    side: Side
    qty: int = Field(gt=0, le=1_000_000)
    product: Product = "DELIVERY"
    trigger: float = Field(gt=0)               # single: the trigger; oco: the target (profit) trigger
    limit: float | None = Field(None, gt=0)    # order price once triggered (None = market)
    stop_trigger: float | None = Field(None, gt=0)
    stop_limit: float | None = Field(None, gt=0)

    _sym = field_validator("symbol")(OrderIn._tradable.__func__)

    @model_validator(mode="after")
    def _oco(self) -> GttIn:
        if self.kind == "oco":
            if self.stop_trigger is None:
                raise ValueError("an OCO needs a stop-loss trigger as well as a target")
            exiting_long = self.side == "SELL"
            if exiting_long and not self.stop_trigger < self.trigger:
                raise ValueError("for a sell OCO the stop-loss must be below the target")
            if not exiting_long and not self.stop_trigger > self.trigger:
                raise ValueError("for a buy OCO (covering a short) the stop-loss must be above the target")
        else:
            self.stop_trigger = self.stop_limit = None
        return self


def gtt_direction(trigger: float, ltp: float) -> str:
    """A single GTT fires when the price reaches the trigger from wherever it was when the GTT was set."""
    return "above" if trigger >= ltp else "below"


def gtt_hit(g: dict, ltp: float) -> str | None:
    """Which leg fires at this price: "target" / "stop" for OCO, "trigger" for single, or None."""
    if g["kind"] == "oco":
        long_exit = g["side"] == "SELL"
        if (ltp >= g["trigger"]) if long_exit else (ltp <= g["trigger"]):
            return "target"
        if (ltp <= g["stop_trigger"]) if long_exit else (ltp >= g["stop_trigger"]):
            return "stop"
        return None
    if g["direction"] == "above":
        return "trigger" if ltp >= g["trigger"] else None
    return "trigger" if ltp <= g["trigger"] else None
