"""Trading API: paper trading (built in) and real trading through the user's own Upstox app.

Paper orders are matched against live prices by a background loop (every 2 s in market hours):
limit / stop orders fill when the price gets there, intraday positions are squared off at 15:20 IST,
and DAY orders that didn't fill expire at the close. Live (Upstox) orders go to the exchange and
always need ``confirm_live: true`` in the request.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import secrets
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from api import deps
from api.live import get_live_provider
from api.options import get_nse
from core import trading as tr
from core.config import get_settings
from core.store import Store
from core.universes import Universes
from tools.errors import ToolError
from tools.providers.base import ProviderError
from tools.providers.nse import NseClient, contract_label
from tools.providers.upstox import UpstoxBroker, UpstoxError

log = logging.getLogger("kairo.trading")
router = APIRouter(tags=["trading"])
OPEN = ("open", "triggered")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def label(symbol: str) -> str:
    return (contract_label(symbol[4:]) or symbol) if tr.is_option(symbol) else symbol


# ------------------------------------------------------------------------------------------ paper engine


class PaperTrader:
    def __init__(self, store: Store, provider: Any, nse: NseClient, now=_now):
        self.store, self.provider, self.nse, self.now = store, provider, nse, now
        self.lock = asyncio.Lock()
        self.task: asyncio.Task | None = None

    # --- prices -------------------------------------------------------------------------------
    async def ltp(self, symbol: str) -> float | None:
        try:
            if tr.is_option(symbol):
                d = await run_in_threadpool(self.nse.contract, symbol[4:], 5)
                return d["ticks"][-1][1]
            return (await deps.quote(symbol, self.provider)).price
        except (ToolError, ProviderError, KeyError, IndexError):
            return None

    async def ltps(self, symbols: set[str]) -> dict[str, float]:
        syms = sorted(symbols)
        vals = await asyncio.gather(*(self.ltp(s) for s in syms))
        return {s: v for s, v in zip(syms, vals) if v is not None}

    async def prev_closes(self, symbols: set[str]) -> dict[str, float]:
        out = {}
        for s in symbols:
            if tr.is_option(s):
                continue
            try:
                q = await deps.quote(s, self.provider)
                if q.previous_close:
                    out[s] = q.previous_close
            except (ToolError, ProviderError):
                pass
        return out

    # --- book ---------------------------------------------------------------------------------
    def start_cash(self) -> float:
        return float(self.store.get_setting("paper_start_cash") or tr.DEFAULT_CASH)

    def book(self) -> tr.Book:
        fills = [{**f, "at": dt.datetime.fromisoformat(f["at"])} for f in self.store.paper_fills()]
        return tr.replay(fills, self.start_cash(), tr.ist(self.now()).date())

    def _fill(self, order: dict, price: float, message: str = "", expect: tuple[str, ...] = OPEN) -> bool:
        fill = {"at": self.now().isoformat(), "symbol": order["symbol"], "side": order["side"], "qty": order["qty"],
                "price": round(price, 4), "product": order["product"]}
        return self.store.fill_paper_order(order["id"], expect, fill, message)

    # --- actions ------------------------------------------------------------------------------
    async def place(self, o: tr.OrderIn) -> dict:
        async with self.lock:
            return await self._place(o)

    async def place_many(self, orders: list[tr.OrderIn]) -> list[dict]:
        """A basket: each order placed in turn (a rejected one doesn't stop the rest), like brokers do."""
        async with self.lock:
            return [await self._place(o) for o in orders]

    async def _place(self, o: tr.OrderIn, note: str = "") -> dict:
        now = self.now()
        open_orders = self.store.paper_orders(OPEN)
        ltps = await self.ltps({o.symbol} | {x["symbol"] for x in open_orders})
        ltp = ltps.get(o.symbol)
        book = self.book()
        row = {**o.model_dump(), "session": tr.order_session(now).isoformat(), "created_at": now.isoformat()}
        why = tr.check_order(o, ltp, book, open_orders, ltps)
        if why:
            oid = self.store.add_paper_order({**row, "status": "rejected", "message": why})
            return self._order(oid)
        oid = self.store.add_paper_order({**row, "status": "open", "message": note})
        await self._match([self._raw(oid)], ltps)
        return self._order(oid)

    # --- GTT / OCO ------------------------------------------------------------------------------
    async def create_gtt(self, g: tr.GttIn) -> dict:
        ltp = await self.ltp(g.symbol)
        if ltp is None:
            raise HTTPException(status_code=503, detail="No live price for this instrument right now")
        if g.kind == "oco":
            wrong = (ltp >= g.trigger or ltp <= g.stop_trigger) if g.side == "SELL" else (ltp <= g.trigger or ltp >= g.stop_trigger)
            if wrong:
                raise HTTPException(status_code=422, detail=f"The price ({ltp:g}) must be between the stop-loss and the target")
        now = self.now()
        gid = self.store.add_gtt({**g.model_dump(), "direction": tr.gtt_direction(g.trigger, ltp) if g.kind == "single" else None,
                                  "expires_at": (now + dt.timedelta(days=365)).isoformat()})
        return view_gtt(next(x for x in self.store.gtts() if x["id"] == gid))

    async def cancel_gtt(self, gtt_id: int) -> bool:
        async with self.lock:
            return self.store.update_gtt(gtt_id, status="cancelled", message="Cancelled by you")

    async def _run_gtts(self, now: dt.datetime) -> None:
        active = self.store.gtts("active")
        for g in active:
            if dt.datetime.fromisoformat(g["expires_at"]) <= now:
                self.store.update_gtt(g["id"], status="expired", message="Expired after a year")
        active = [g for g in self.store.gtts("active")]
        if not active or not tr.market_open(now):
            return
        ltps = await self.ltps({g["symbol"] for g in active})
        for g in active:
            ltp = ltps.get(g["symbol"])
            leg = tr.gtt_hit(g, ltp) if ltp is not None else None
            if leg is None:
                continue
            price = g["stop_limit"] if leg == "stop" else g["limit_price"]
            o = tr.OrderIn(symbol=g["symbol"], side=g["side"], qty=int(g["qty"]), product=g["product"],
                           order_type="LIMIT" if price else "MARKET", price=price)
            what = {"target": "target", "stop": "stop-loss", "trigger": "trigger"}[leg]
            # Claim the GTT first so a slow order can't make it fire twice.
            if not self.store.update_gtt(g["id"], status="triggered", message=f"{what.capitalize()} hit at {ltp:g}"):
                continue
            order = await self._place(o, note=f"From GTT #{g['id']} ({what})")
            self.store.update_gtt(g["id"], expect="triggered", order_id=order["id"],
                                  message=f"{what.capitalize()} hit at {ltp:g}; order {order['status']}" + ("; the other leg was cancelled" if g["kind"] == "oco" else ""))

    def gtts(self) -> list[dict]:
        return [view_gtt(g) for g in self.store.gtts()]

    async def cancel(self, order_id: int) -> bool:
        async with self.lock:
            return self.store.update_paper_order(order_id, expect=OPEN, status="cancelled", message="Cancelled by you")

    async def exit(self, symbol: str, product: str) -> dict:
        ln = self.book().lines.get((symbol, product))
        if not ln or abs(ln.qty) < 1e-9:
            raise HTTPException(status_code=404, detail="no open position to exit")
        return await self.place(tr.OrderIn(symbol=symbol, side="SELL" if ln.qty > 0 else "BUY", qty=int(abs(ln.qty)),
                                           order_type="MARKET", product=product))

    def _raw(self, order_id: int) -> dict:
        return next(x for x in self.store.paper_orders() if x["id"] == order_id)

    def _order(self, order_id: int) -> dict:
        return view_order(self._raw(order_id))

    async def _match(self, orders: list[dict], ltps: dict[str, float]) -> None:
        now = self.now()
        if not tr.market_open(now):
            return
        today = tr.ist(now).date().isoformat()
        for o in orders:
            if o["session"] != today or o["status"] not in OPEN:
                continue
            ltp = ltps.get(o["symbol"])
            if ltp is None:
                continue
            status, price = tr.match(o, ltp)
            if price is not None:
                self._fill(o, price, expect=(o["status"],))
            elif status != o["status"]:
                self.store.update_paper_order(o["id"], expect=(o["status"],), status=status, message="Trigger hit; waiting at the limit price")

    async def tick(self) -> None:
        """One pass: expire stale orders, fire GTTs, match open orders, square off intraday at 15:20."""
        async with self.lock:
            now = self.now()
            await self._run_gtts(now)
            t = tr.ist(now)
            today = t.date()
            for o in self.store.paper_orders(OPEN):
                session = dt.date.fromisoformat(o["session"])
                if session < today or (session == today and t.time() >= tr.SESSION_CLOSE):
                    self.store.update_paper_order(o["id"], expect=OPEN, status="cancelled", message="Expired at the end of the day")
            open_orders = self.store.paper_orders(OPEN)
            book = self.book()
            stale_intraday = tr.to_square_off(book)
            due = tr.is_trading_day(today) and tr.SQUARE_OFF <= t.time() < tr.SESSION_CLOSE
            symbols = {o["symbol"] for o in open_orders} | {ln.symbol for ln in stale_intraday}
            if not symbols:
                return
            ltps = await self.ltps(symbols)
            await self._match(open_orders, ltps)
            # Intraday positions: closed at 15:20, or as soon as we can if one was left from an earlier day.
            left_over = any(ln.realised_today == 0 and not ln.day_buy_qty and not ln.day_sell_qty for ln in stale_intraday)
            if due or (left_over and tr.market_open(now)):
                for o in self.store.paper_orders(OPEN):
                    if o["product"] == "INTRADAY":
                        self.store.update_paper_order(o["id"], expect=OPEN, status="cancelled", message="Cancelled at intraday square-off")
                for ln in tr.to_square_off(self.book()):
                    ltp = ltps.get(ln.symbol)
                    if ltp is None:
                        continue
                    row = {"symbol": ln.symbol, "side": "SELL" if ln.qty > 0 else "BUY", "qty": abs(ln.qty), "order_type": "MARKET",
                           "product": "INTRADAY", "status": "open", "session": today.isoformat(), "created_at": now.isoformat()}
                    oid = self.store.add_paper_order(row)
                    self._fill({**row, "id": oid}, ltp, "Auto square-off at 3:20 pm")

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("paper trading tick failed")
            await asyncio.sleep(2 if tr.market_open(self.now()) else 30)

    def ensure_running(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.get_running_loop().create_task(self.run())

    # --- views ----------------------------------------------------------------------------------
    async def positions(self) -> list[dict]:
        book = self.book()
        ltps = await self.ltps({ln.symbol for ln in book.lines.values()})
        return [{**r, "label": label(r["symbol"])} for r in tr.position_rows(book, ltps)]

    async def holdings(self) -> list[dict]:
        book = self.book()
        syms = {ln.symbol for ln in book.lines.values() if ln.qty > 0}
        ltps, prev = await self.ltps(syms), await self.prev_closes(syms)
        return [{**r, "label": label(r["symbol"])} for r in tr.holding_rows(book, ltps, prev)]

    async def funds(self) -> dict:
        book, open_orders = self.book(), self.store.paper_orders(OPEN)
        ltps = await self.ltps({o["symbol"] for o in open_orders} | {ln.symbol for ln in book.lines.values() if ln.qty})
        f = tr.funds(book, open_orders, ltps)
        unreal = sum((ltps[ln.symbol] - ln.avg) * ln.qty for ln in book.lines.values() if ln.qty and ln.symbol in ltps)
        value = sum(ltps[ln.symbol] * ln.qty for ln in book.lines.values()
                    if ln.qty and tr.cash_product(ln.symbol, ln.product) and ln.symbol in ltps)
        return {**f, "unrealised": round(unreal, 2), "realised": round(sum(ln.realised for ln in book.lines.values()), 2),
                "account_value": round(book.cash + value + sum((ltps[ln.symbol] - ln.avg) * ln.qty for ln in book.lines.values()
                                                                 if ln.qty and not tr.cash_product(ln.symbol, ln.product) and ln.symbol in ltps), 2)}

    def orders(self) -> list[dict]:
        return [view_order(o) for o in self.store.paper_orders()]

    def reset(self, start_cash: float) -> None:
        self.store.reset_paper()
        self.store.set_setting("paper_start_cash", str(start_cash))


def view_gtt(g: dict) -> dict:
    return {"id": g["id"], "symbol": g["symbol"], "label": label(g["symbol"]), "kind": g["kind"], "side": g["side"], "qty": g["qty"],
            "product": g["product"], "trigger": g["trigger"], "limit": g["limit_price"], "stop_trigger": g["stop_trigger"],
            "stop_limit": g["stop_limit"], "status": g["status"], "order_id": g["order_id"], "message": g["message"],
            "created_at": g["created_at"], "expires_at": g["expires_at"]}


def view_order(o: dict) -> dict:
    return {"id": o["id"], "symbol": o["symbol"], "label": label(o["symbol"]), "side": o["side"], "qty": o["qty"],
            "order_type": o["order_type"], "price": o["price"], "trigger_price": o["trigger_price"], "product": o["product"],
            "status": o["status"], "fill_price": o["fill_price"], "message": o["message"], "created_at": o["created_at"]}


_TRADERS: dict[int, PaperTrader] = {}


def get_paper(store: Store = Depends(deps.get_store), provider=Depends(get_live_provider),
              nse: NseClient = Depends(get_nse)) -> PaperTrader:
    trader = _TRADERS.get(id(store))
    if trader is None:
        trader = _TRADERS[id(store)] = PaperTrader(store, provider, nse)
    return trader


# ------------------------------------------------------------------------------------------ Upstox


_STATES: set[str] = set()


def upstox_expiry(created: dt.datetime) -> dt.datetime:
    """Upstox tokens die at 03:30 IST: the same day if made before 03:30, else the next day."""
    t = tr.ist(created)
    cut = dt.datetime.combine(t.date(), dt.time(3, 30), tzinfo=tr.IST)
    return cut if t < cut else cut + dt.timedelta(days=1)


def get_upstox(store: Store = Depends(deps.get_store)) -> UpstoxBroker | None:
    s = get_settings()
    if not (s.upstox_api_key and s.upstox_api_secret):
        return None
    token, created = store.get_setting("upstox_token"), store.get_setting("upstox_token_at")
    if token and created and _now() >= upstox_expiry(dt.datetime.fromisoformat(created)):
        token = None
    return UpstoxBroker(s.upstox_api_key.get_secret_value(), s.upstox_api_secret.get_secret_value(), s.upstox_redirect_uri, token)


def mode_of(store: Store, upstox: UpstoxBroker | None) -> str:
    return "upstox" if store.get_setting("trading_mode") == "upstox" and upstox and upstox.token else "paper"


def _live_call(fn, *args):
    try:
        return fn(*args)
    except UpstoxError as exc:
        raise HTTPException(status_code=401 if exc.expired else 502, detail=f"Upstox: {exc}") from None


# ------------------------------------------------------------------------------------------ endpoints


class OrderRequest(BaseModel):
    symbol: str
    side: tr.Side
    qty: int
    order_type: tr.OrderType = "MARKET"
    price: float | None = None
    trigger_price: float | None = None
    product: tr.Product = "DELIVERY"
    confirm_live: bool = False
    label: str | None = Field(None, max_length=80)   # display name kept with saved baskets


class ModeRequest(BaseModel):
    mode: Literal["paper", "upstox"]


class ResetRequest(BaseModel):
    start_cash: float = Field(tr.DEFAULT_CASH, ge=10_000, le=100_000_000)


class BasketRequest(BaseModel):
    orders: list[OrderRequest] = Field(min_length=1, max_length=20)
    confirm_live: bool = False


class SavedBasket(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    orders: list[OrderRequest] = Field(default_factory=list, max_length=20)


class ExitRequest(BaseModel):
    symbol: str
    product: tr.Product


@router.get("/trading/status")
async def status(store: Store = Depends(deps.get_store), paper: PaperTrader = Depends(get_paper),
                 upstox: UpstoxBroker | None = Depends(get_upstox)) -> dict[str, Any]:
    paper.ensure_running()
    created = store.get_setting("upstox_token_at")
    return {"mode": mode_of(store, upstox), "market_open": tr.market_open(_now()),
            "paper": {"start_cash": paper.start_cash()},
            "upstox": {"configured": upstox is not None, "connected": bool(upstox and upstox.token),
                       "user": store.get_setting("upstox_user"),
                       "expires_at": upstox_expiry(dt.datetime.fromisoformat(created)).isoformat() if created and upstox and upstox.token else None}}


@router.post("/trading/mode")
async def set_mode(body: ModeRequest, store: Store = Depends(deps.get_store), upstox: UpstoxBroker | None = Depends(get_upstox)) -> dict[str, str]:
    if body.mode == "upstox" and not (upstox and upstox.token):
        raise HTTPException(status_code=409, detail="Connect your Upstox account first")
    store.set_setting("trading_mode", body.mode)
    return {"mode": body.mode}


@router.post("/trading/orders")
async def place(body: OrderRequest, store: Store = Depends(deps.get_store), paper: PaperTrader = Depends(get_paper),
                upstox: UpstoxBroker | None = Depends(get_upstox), registry: Universes = Depends(deps.get_universes)) -> dict[str, Any]:
    paper.ensure_running()
    try:
        order = tr.OrderIn(**body.model_dump(exclude={"confirm_live", "label"}))
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors())) from None
    if mode_of(store, upstox) == "paper":
        return {"mode": "paper", "order": await paper.place(order)}
    if not body.confirm_live:
        raise HTTPException(status_code=428, detail="Live order not confirmed")
    if tr.is_option(order.symbol):
        raise HTTPException(status_code=422, detail="Options can't be traded through Upstox here yet; switch to paper mode for options")
    isin = (await run_in_threadpool(registry.venues, order.symbol)).get("isin")
    if not isin:
        raise HTTPException(status_code=422, detail=f"No ISIN found for {order.symbol}")
    ids = await run_in_threadpool(_live_call, upstox.place, order.model_dump(), isin, tr.market_open(_now()))
    return {"mode": "upstox", "order_ids": ids}


@router.delete("/trading/orders/{order_id}")
async def cancel(order_id: str, store: Store = Depends(deps.get_store), paper: PaperTrader = Depends(get_paper),
                 upstox: UpstoxBroker | None = Depends(get_upstox)) -> dict[str, Any]:
    if mode_of(store, upstox) == "upstox":
        await run_in_threadpool(_live_call, upstox.cancel, order_id)
        return {"cancelled": order_id}
    if not order_id.isdigit() or not await paper.cancel(int(order_id)):
        raise HTTPException(status_code=409, detail="That order can't be cancelled (already filled, cancelled or unknown)")
    return {"cancelled": order_id}


@router.get("/trading/orders")
async def orders(store: Store = Depends(deps.get_store), paper: PaperTrader = Depends(get_paper),
                 upstox: UpstoxBroker | None = Depends(get_upstox)) -> dict[str, Any]:
    paper.ensure_running()
    if mode_of(store, upstox) == "upstox":
        return {"mode": "upstox", "items": await run_in_threadpool(_live_call, upstox.orders)}
    return {"mode": "paper", "items": paper.orders()}


@router.get("/trading/positions")
async def positions(store: Store = Depends(deps.get_store), paper: PaperTrader = Depends(get_paper),
                    upstox: UpstoxBroker | None = Depends(get_upstox)) -> dict[str, Any]:
    if mode_of(store, upstox) == "upstox":
        return {"mode": "upstox", "items": await run_in_threadpool(_live_call, upstox.positions)}
    return {"mode": "paper", "items": await paper.positions()}


@router.get("/trading/holdings")
async def holdings(store: Store = Depends(deps.get_store), paper: PaperTrader = Depends(get_paper),
                   upstox: UpstoxBroker | None = Depends(get_upstox)) -> dict[str, Any]:
    if mode_of(store, upstox) == "upstox":
        return {"mode": "upstox", "items": await run_in_threadpool(_live_call, upstox.holdings)}
    return {"mode": "paper", "items": await paper.holdings()}


@router.get("/trading/funds")
async def funds(store: Store = Depends(deps.get_store), paper: PaperTrader = Depends(get_paper),
                upstox: UpstoxBroker | None = Depends(get_upstox)) -> dict[str, Any]:
    if mode_of(store, upstox) == "upstox":
        return {"mode": "upstox", **await run_in_threadpool(_live_call, upstox.funds)}
    return {"mode": "paper", **await paper.funds()}


def _orders_in(reqs: list[OrderRequest]) -> list[tr.OrderIn]:
    out = []
    for i, r in enumerate(reqs, 1):
        try:
            out.append(tr.OrderIn(**r.model_dump(exclude={"confirm_live", "label"})))
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=f"Order {i}: " + "; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors())) from None
    return out


@router.post("/trading/basket")
async def place_basket(body: BasketRequest, store: Store = Depends(deps.get_store), paper: PaperTrader = Depends(get_paper),
                       upstox: UpstoxBroker | None = Depends(get_upstox), registry: Universes = Depends(deps.get_universes)) -> dict[str, Any]:
    """Place up to 20 orders at once. Each goes in on its own; one rejection doesn't stop the others."""
    paper.ensure_running()
    orders = _orders_in(body.orders)
    if mode_of(store, upstox) == "paper":
        return {"mode": "paper", "orders": await paper.place_many(orders)}
    if not body.confirm_live:
        raise HTTPException(status_code=428, detail="Live basket not confirmed")
    results = []
    for o in orders:
        if tr.is_option(o.symbol):
            results.append({"symbol": o.symbol, "status": "rejected", "message": "Options aren't wired to Upstox yet"}); continue
        isin = (await run_in_threadpool(registry.venues, o.symbol)).get("isin")
        try:
            ids = await run_in_threadpool(upstox.place, o.model_dump(), isin, tr.market_open(_now())) if isin else None
            results.append({"symbol": o.symbol, "status": "sent" if ids else "rejected", "order_ids": ids or [],
                            "message": "" if ids else "No ISIN for this symbol"})
        except UpstoxError as exc:
            results.append({"symbol": o.symbol, "status": "rejected", "message": str(exc)})
    return {"mode": "upstox", "orders": results}


@router.get("/trading/baskets")
async def list_baskets(store: Store = Depends(deps.get_store)) -> dict[str, Any]:
    return {"items": store.baskets()}


@router.post("/trading/baskets")
async def save_basket(body: SavedBasket, basket_id: int | None = Query(None), store: Store = Depends(deps.get_store)) -> dict[str, Any]:
    _orders_in(body.orders) if body.orders else None
    bid = store.save_basket(body.name, [o.model_dump(exclude={"confirm_live"}, exclude_none=True) for o in body.orders], basket_id)
    return next(b for b in store.baskets() if b["id"] == bid)


@router.delete("/trading/baskets/{basket_id}")
async def delete_basket(basket_id: int, store: Store = Depends(deps.get_store)) -> dict[str, Any]:
    if not store.delete_basket(basket_id):
        raise HTTPException(status_code=404, detail="no such basket")
    return {"deleted": basket_id}


@router.get("/trading/gtt")
async def list_gtt(paper: PaperTrader = Depends(get_paper)) -> dict[str, Any]:
    paper.ensure_running()
    return {"items": paper.gtts()}


@router.post("/trading/gtt")
async def create_gtt(body: dict[str, Any], store: Store = Depends(deps.get_store), paper: PaperTrader = Depends(get_paper),
                     upstox: UpstoxBroker | None = Depends(get_upstox)) -> dict[str, Any]:
    if mode_of(store, upstox) == "upstox":
        raise HTTPException(status_code=409, detail="GTT orders work in paper mode for now")
    try:
        g = tr.GttIn(**body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors())) from None
    paper.ensure_running()
    return await paper.create_gtt(g)


@router.delete("/trading/gtt/{gtt_id}")
async def delete_gtt(gtt_id: int, paper: PaperTrader = Depends(get_paper)) -> dict[str, Any]:
    if not await paper.cancel_gtt(gtt_id):
        raise HTTPException(status_code=409, detail="That GTT isn't active")
    return {"cancelled": gtt_id}


@router.post("/trading/exit")
async def exit_position(body: ExitRequest, store: Store = Depends(deps.get_store), paper: PaperTrader = Depends(get_paper),
                        upstox: UpstoxBroker | None = Depends(get_upstox)) -> dict[str, Any]:
    if mode_of(store, upstox) == "upstox":
        raise HTTPException(status_code=409, detail="Exit live positions with a confirmed order from the order ticket")
    return {"mode": "paper", "order": await paper.exit(body.symbol, body.product)}


@router.post("/trading/paper/reset")
async def reset(body: ResetRequest, paper: PaperTrader = Depends(get_paper)) -> dict[str, Any]:
    async with paper.lock:
        paper.reset(body.start_cash)
    return {"start_cash": body.start_cash}


@router.get("/broker/upstox/login")
async def upstox_login(upstox: UpstoxBroker | None = Depends(get_upstox)) -> RedirectResponse:
    if upstox is None:
        raise HTTPException(status_code=409, detail="Add UPSTOX_API_KEY and UPSTOX_API_SECRET to .env first")
    state = secrets.token_urlsafe(16)
    _STATES.add(state)
    return RedirectResponse(upstox.login_url(state))


@router.get("/broker/upstox/callback")
async def upstox_callback(code: str = Query("", max_length=200), state: str = Query("", max_length=100),
                          store: Store = Depends(deps.get_store), upstox: UpstoxBroker | None = Depends(get_upstox)) -> RedirectResponse:
    if upstox is None or state not in _STATES or not code:
        return RedirectResponse("/#/terminal?broker=failed")
    _STATES.discard(state)
    try:
        info = await run_in_threadpool(upstox.exchange, code)
    except UpstoxError as exc:
        log.warning("Upstox login failed: %s", exc)
        return RedirectResponse("/#/terminal?broker=failed")
    store.set_setting("upstox_token", info["token"])
    store.set_setting("upstox_token_at", _now().isoformat())
    store.set_setting("upstox_user", info.get("user_name") or info.get("user_id") or "")
    return RedirectResponse("/#/terminal?broker=connected")


@router.post("/broker/upstox/logout")
async def upstox_logout(store: Store = Depends(deps.get_store)) -> dict[str, bool]:
    for key in ("upstox_token", "upstox_token_at", "upstox_user"):
        store.set_setting(key, None)
    store.set_setting("trading_mode", "paper")
    return {"connected": False}
