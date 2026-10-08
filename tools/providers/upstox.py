"""Upstox connector for real trading (stocks), using the user's own free Upstox developer app.

Flow: the user opens ``login_url()`` -> logs in at Upstox -> Upstox redirects back with ``code`` ->
``exchange(code)`` returns an access token, valid until 3:30 AM IST the next day (Upstox's rule).

Endpoints (developer docs, v2 login / v3 orders):
  dialog   GET  https://api.upstox.com/v2/login/authorization/dialog
  token    POST https://api.upstox.com/v2/login/authorization/token   (form-encoded)
  place    POST https://api-hft.upstox.com/v3/order/place
  cancel   DEL  https://api-hft.upstox.com/v3/order/cancel?order_id=
  orders   GET  https://api.upstox.com/v2/order/retrieve-all
  positions GET https://api.upstox.com/v2/portfolio/short-term-positions
  holdings GET  https://api.upstox.com/v2/portfolio/long-term-holdings
  funds    GET  https://api.upstox.com/v2/user/get-funds-and-margin?segment=SEC

Instruments are addressed by exchange and ISIN (``NSE_EQ|INE002A01018``). Options need Upstox's
instrument master and aren't wired yet, so live orders are stocks only.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

API = "https://api.upstox.com/v2"
HFT = "https://api-hft.upstox.com/v3"
PRODUCT = {"INTRADAY": "I", "DELIVERY": "D"}
PRODUCT_BACK = {"I": "INTRADAY", "D": "DELIVERY", "MTF": "MTF", "CO": "CO"}


class UpstoxError(Exception):
    def __init__(self, message: str, status: int = 0, code: str | None = None):
        super().__init__(message)
        self.status, self.code = status, code

    @property
    def expired(self) -> bool:
        return self.status == 401


def instrument_token(symbol: str, isin: str) -> str:
    return f"{'BSE_EQ' if symbol.endswith('.BO') else 'NSE_EQ'}|{isin}"


def app_symbol(exchange: str | None, trading_symbol: str | None, token: str | None = None) -> str:
    """Upstox (exchange, trading symbol) -> the app's symbol (RELIANCE.NS), or the raw name for derivatives."""
    ts = (trading_symbol or "").strip().upper()
    seg = (token or "").split("|")[0] or (exchange or "")
    if seg in ("NSE_EQ", "NSE") and ts:
        return f"{ts.removesuffix('-EQ')}.NS"
    if seg in ("BSE_EQ", "BSE") and ts:
        return f"{ts}.BO"
    return ts or (token or "?")


class UpstoxBroker:
    def __init__(self, api_key: str, api_secret: str, redirect_uri: str, token: str | None = None,
                 client: httpx.Client | None = None):
        self.api_key, self.api_secret, self.redirect_uri, self.token = api_key, api_secret, redirect_uri, token
        self.client = client or httpx.Client(timeout=10)

    # --- auth -----------------------------------------------------------------------------------

    def login_url(self, state: str) -> str:
        q = {"client_id": self.api_key, "redirect_uri": self.redirect_uri, "response_type": "code", "state": state}
        return f"{API}/login/authorization/dialog?{urlencode(q)}"

    def exchange(self, code: str) -> dict[str, Any]:
        r = self._send("POST", f"{API}/login/authorization/token", auth=False,
                       data={"code": code, "client_id": self.api_key, "client_secret": self.api_secret,
                             "redirect_uri": self.redirect_uri, "grant_type": "authorization_code"},
                       headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
        token = r.get("access_token")
        if not token:
            raise UpstoxError("Upstox didn't return an access token")
        self.token = token
        return {"token": token, "user_id": r.get("user_id"), "user_name": r.get("user_name")}

    # --- http -------------------------------------------------------------------------------------

    def _send(self, method: str, url: str, auth: bool = True, **kw: Any) -> dict[str, Any]:
        headers = {"Accept": "application/json", **kw.pop("headers", {})}
        if auth:
            if not self.token:
                raise UpstoxError("Not connected to Upstox", status=401)
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            r = self.client.request(method, url, headers=headers, **kw)
        except httpx.HTTPError as exc:
            raise UpstoxError(f"Upstox unreachable: {type(exc).__name__}") from exc
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code >= 400 or body.get("status") == "error":
            errs = body.get("errors") or [{}]
            first = errs[0] if isinstance(errs, list) and errs else {}
            msg = first.get("message") or body.get("message") or f"Upstox returned HTTP {r.status_code}"
            raise UpstoxError(msg, status=r.status_code, code=first.get("errorCode") or body.get("error_code"))
        return body

    def _data(self, method: str, url: str, **kw: Any) -> Any:
        return self._send(method, url, **kw).get("data")

    # --- trading ----------------------------------------------------------------------------------

    def place(self, order: dict[str, Any], isin: str, market_open: bool) -> list[str]:
        body = {
            "quantity": int(order["qty"]), "product": PRODUCT[order["product"]], "validity": "DAY",
            "price": float(order.get("price") or 0), "trigger_price": float(order.get("trigger_price") or 0),
            "instrument_token": instrument_token(order["symbol"], isin), "order_type": order["order_type"],
            "transaction_type": order["side"], "disclosed_quantity": 0,
            "is_amo": not market_open,  # outside market hours Upstox needs an after-market order
            "tag": "kairo", "slice": False,
        }
        data = self._data("POST", f"{HFT}/order/place", json=body, headers={"Content-Type": "application/json"}) or {}
        ids = data.get("order_ids") or ([data["order_id"]] if data.get("order_id") else [])
        if not ids:
            raise UpstoxError("Upstox accepted the request but returned no order id")
        return [str(i) for i in ids]

    def cancel(self, order_id: str) -> None:
        self._data("DELETE", f"{HFT}/order/cancel", params={"order_id": order_id})

    def orders(self) -> list[dict[str, Any]]:
        out = []
        for o in self._data("GET", f"{API}/order/retrieve-all") or []:
            out.append({"id": o.get("order_id"), "symbol": app_symbol(o.get("exchange"), o.get("trading_symbol"), o.get("instrument_token")),
                        "side": o.get("transaction_type"), "qty": o.get("quantity"), "filled_qty": o.get("filled_quantity"),
                        "order_type": o.get("order_type"), "product": PRODUCT_BACK.get(o.get("product"), o.get("product")),
                        "price": o.get("price") or None, "trigger_price": o.get("trigger_price") or None,
                        "fill_price": o.get("average_price") or None, "status": (o.get("status") or "").lower(),
                        "message": o.get("status_message") or "", "created_at": o.get("order_timestamp")})
        return out

    def positions(self) -> list[dict[str, Any]]:
        out = []
        for p in self._data("GET", f"{API}/portfolio/short-term-positions") or []:
            out.append({"symbol": app_symbol(p.get("exchange"), p.get("trading_symbol"), p.get("instrument_token")),
                        "product": PRODUCT_BACK.get(p.get("product"), p.get("product")), "qty": p.get("quantity") or 0,
                        "avg": p.get("average_price"), "ltp": p.get("last_price"), "realised": p.get("realised"),
                        "unrealised": p.get("unrealised"), "pnl": p.get("pnl"),
                        "buy_qty": p.get("day_buy_quantity"), "sell_qty": p.get("day_sell_quantity"),
                        "buy_avg": p.get("buy_price"), "sell_avg": p.get("sell_price")})
        return out

    def holdings(self) -> list[dict[str, Any]]:
        out = []
        for h in self._data("GET", f"{API}/portfolio/long-term-holdings") or []:
            qty, avg, ltp, close = h.get("quantity") or 0, h.get("average_price"), h.get("last_price"), h.get("close_price")
            out.append({"symbol": app_symbol(h.get("exchange"), h.get("trading_symbol"), h.get("instrument_token")),
                        "name": h.get("company_name"), "qty": qty, "avg": avg, "ltp": ltp,
                        "invested": round(qty * avg, 2) if avg is not None else None,
                        "value": round(qty * ltp, 2) if ltp is not None else None, "pnl": h.get("pnl"),
                        "day_change": round((ltp - close) * qty, 2) if ltp is not None and close else None})
        return out

    def funds(self) -> dict[str, Any]:
        eq = (self._data("GET", f"{API}/user/get-funds-and-margin", params={"segment": "SEC"}) or {}).get("equity") or {}
        return {"available": eq.get("available_margin"), "margin_used": eq.get("used_margin"),
                "payin": eq.get("payin_amount"), "cash": None, "start_cash": None, "reserved": None}
