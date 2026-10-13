"""Portfolio API: transactions, live valuation with XIRR, tax P&L, health check, CSV and CAS import."""

from __future__ import annotations

import asyncio
import base64
import binascii
import datetime as dt
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from api import deps
from api.live import get_live_provider
from core import cas
from core import wealth
from core.health import HoldingInput, build_health
from core.importers import parse_csv
from core.mutual_funds import classify as classify_fund
from core.portfolio import Transaction, TransactionIn, build_book, value_portfolio
from core.store import Store
from core.tax_india import available_fys, fy_of, fy_report, tax_planning
from tools.errors import ToolError, UnknownSymbolError
from tools.models import is_mf_symbol, mf_code
from tools.providers.base import ProviderError
from tools.providers import MarketDataProvider

router = APIRouter(prefix="/portfolio", tags=["portfolio"])
BASE_CURRENCY = "INR"
BENCHMARKS = {"INR": ("NIFTY 50", "^NSEI"), "USD": ("S&P 500", "^GSPC")}


def _transactions(store: Store) -> list[Transaction]:
    return [Transaction(**r) for r in store.list_transactions()]


async def _fx_to_base(currencies: set[str], provider: MarketDataProvider) -> tuple[dict[str, float], list[str]]:
    rates, missing = {}, []
    for cur in sorted(c for c in currencies if c and c not in (BASE_CURRENCY, "?")):
        try:
            rates[cur] = (await deps.quote(f"{cur}{BASE_CURRENCY}=X", provider)).price
        except ToolError:
            missing.append(cur)
    return rates, missing


@router.get("")
async def portfolio(store: Store = Depends(deps.get_store),
                    provider: MarketDataProvider = Depends(get_live_provider)) -> dict[str, Any]:
    book = build_book(_transactions(store))
    open_syms = book.open_symbols()
    quotes, quote_errors = await deps.quotes_for(open_syms, provider)
    currencies = {q.currency for q in quotes.values() if q.currency} | {b.currency for b in book.symbols.values() if b.currency}
    fx, fx_missing = await _fx_to_base(currencies, provider)
    view = value_portfolio(book, quotes, today=dt.date.today(), base_currency=BASE_CURRENCY, fx_to_base=fx)
    return {**view.model_dump(mode="json"), "quote_errors": quote_errors, "fx_missing": fx_missing,
            "transactions": len(store.list_transactions())}


@router.get("/transactions")
def list_transactions(store: Store = Depends(deps.get_store)) -> list[dict[str, Any]]:
    return [Transaction(**r).model_dump(mode="json") for r in reversed(store.list_transactions())]


async def _currency_or_404(symbol: str, provider: MarketDataProvider) -> str | None:
    try:
        return (await deps.quote(symbol, provider)).currency
    except UnknownSymbolError:
        raise HTTPException(status_code=404, detail=f"Unknown symbol {symbol}. NSE stocks end in .NS, BSE in .BO.") from None
    except ToolError:
        return None  # data source hiccup: accept the trade, currency fills in later


@router.post("/transactions", status_code=201)
async def add_transaction(txn: TransactionIn, store: Store = Depends(deps.get_store),
                          provider: MarketDataProvider = Depends(get_live_provider)) -> dict[str, Any]:
    currency = await _currency_or_404(txn.symbol, provider)
    (new_id,) = store.add_transactions([{**txn.model_dump(), "currency": currency}])
    return Transaction(id=new_id, currency=currency, **txn.model_dump()).model_dump(mode="json")


@router.delete("/transactions/{txn_id}", status_code=204)
def delete_transaction(txn_id: int, store: Store = Depends(deps.get_store)) -> None:
    if not store.delete_transaction(txn_id):
        raise HTTPException(status_code=404, detail="no such transaction")


@router.delete("/transactions", status_code=200)
def clear_transactions(confirm: bool = Query(False), store: Store = Depends(deps.get_store)) -> dict[str, int]:
    if not confirm:
        raise HTTPException(status_code=400, detail="pass confirm=true to delete every transaction")
    return {"deleted": store.clear_transactions()}


class ImportBody(BaseModel):
    csv: str = Field(min_length=1, max_length=5_000_000)
    dry_run: bool = False


@router.post("/import")
async def import_csv(body: ImportBody = Body(...), store: Store = Depends(deps.get_store),
                     provider: MarketDataProvider = Depends(get_live_provider)) -> dict[str, Any]:
    parsed = parse_csv(body.csv)
    symbols = sorted({r.symbol for r in parsed.rows})
    results = await deps.gather_limited(symbols, lambda s: deps.quote(s, provider))
    currency: dict[str, str | None] = {}
    unknown: set[str] = set()
    for sym, res in zip(symbols, results):
        if isinstance(res, UnknownSymbolError):
            unknown.add(sym)
        elif isinstance(res, BaseException):
            currency[sym] = None
        else:
            currency[sym] = res.currency
    rows = [r for r in parsed.rows if r.symbol not in unknown]
    errors = [e.model_dump() for e in parsed.errors] + [
        {"line": None, "message": f"unknown symbol {s} - rows skipped"} for s in sorted(unknown)]
    preview = [{**r.model_dump(mode="json"), "currency": currency.get(r.symbol)} for r in rows]
    if body.dry_run:
        return {"format": parsed.format, "rows": preview, "errors": errors, "skipped": parsed.skipped, "imported": 0}
    ids = store.add_transactions([{**r.model_dump(), "currency": currency.get(r.symbol)} for r in rows],
                                 source=f"import:{parsed.format}")
    return {"format": parsed.format, "rows": preview, "errors": errors, "skipped": parsed.skipped,
            "imported": len(ids)}


class CasBody(BaseModel):
    pdf_base64: str = Field(min_length=10, max_length=20_000_000)
    password: str = Field("", max_length=64)
    dry_run: bool = True


@router.post("/import-cas")
async def import_cas(body: CasBody, store: Store = Depends(deps.get_store),
                     provider: MarketDataProvider = Depends(get_live_provider)) -> dict[str, Any]:
    """A CAMS / KFintech consolidated account statement (PDF) -> mutual-fund transactions. Preview with
    ``dry_run`` (the default), then import. Rows already in the portfolio are skipped, so importing twice is safe."""
    funds = getattr(provider, "funds", None)
    if funds is None:
        raise HTTPException(status_code=503, detail="mutual-fund data is not configured")
    try:
        data = base64.b64decode(body.pdf_base64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=422, detail="the file didn't arrive intact; try again") from None
    try:
        schemes = await run_in_threadpool(lambda: cas.parse_cas(cas.read_pdf_text(data, body.password.strip())))
        catalog = await run_in_threadpool(funds.data.catalog)
    except cas.CasError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail=f"AMFI scheme list unavailable: {exc}") from None
    out = cas.to_transactions(schemes, catalog)
    have = {(t["symbol"], t["side"], round(t["quantity"], 3), round(t["price"], 3), str(t["trade_date"])) for t in store.list_transactions()}
    fresh, dupes = [], 0
    for r in out["rows"]:
        if (r["symbol"], r["side"], round(r["quantity"], 3), round(r["price"], 3), r["trade_date"]) in have:
            dupes += 1
        else:
            fresh.append(r)
    valid, errors = [], list(out["errors"])
    for r in fresh:
        try:
            valid.append(TransactionIn(**r))
        except ValueError as exc:
            errors.append(f"{r['symbol']} {r['trade_date']}: {exc}")
    imported = 0
    if not body.dry_run and valid:
        imported = len(store.add_transactions([{**v.model_dump(), "currency": "INR"} for v in valid], source="import:cas"))
    return {"format": "cas", "schemes": out["schemes"], "rows": [v.model_dump(mode="json") for v in valid], "errors": errors,
            "already_imported": dupes, "imported": imported}


async def _mf_kinds(symbols: list[str], provider: MarketDataProvider) -> dict[str, str]:
    """Tax kind (equity / debt / other) for each mutual-fund symbol, from its AMFI category."""
    funds = getattr(provider, "funds", None)
    out: dict[str, str] = {}
    for sym in symbols:
        if not is_mf_symbol(sym) or funds is None:
            continue
        try:
            scheme = await run_in_threadpool(funds.data.scheme, mf_code(sym))
            out[sym] = classify_fund(scheme.category, scheme.name).tax_kind
        except ProviderError:
            continue
    return out


@router.get("/tax")
async def tax(fy: str | None = Query(None, pattern=r"^FY \d{4}-\d{2}$"), store: Store = Depends(deps.get_store),
              provider: MarketDataProvider = Depends(get_live_provider)) -> dict[str, Any]:
    today = dt.date.today()
    book = build_book(_transactions(store))
    fy = fy or fy_of(today)
    quotes, _ = await deps.quotes_for(book.open_symbols(), provider)
    kinds = await _mf_kinds(list(book.symbols), provider)
    plan = tax_planning(book, {s: q.price for s, q in quotes.items()}, today, kinds.get)
    return {"fy": fy, "available_fys": available_fys(book, today),
            "report": fy_report(book, fy, kinds.get).model_dump(mode="json"), "planning": plan.model_dump(mode="json")}


@router.get("/harvest")
async def harvest(store: Store = Depends(deps.get_store), provider: MarketDataProvider = Depends(get_live_provider)) -> dict[str, Any]:
    """Tax-harvesting plan for this financial year: gains to book within the LTCG exemption, losses to book against gains."""
    today = dt.date.today()
    book = build_book(_transactions(store))
    quotes, _ = await deps.quotes_for(book.open_symbols(), provider)
    kinds = await _mf_kinds(list(book.symbols), provider)
    prices = {s: q.price for s, q in quotes.items()}
    rep_ = fy_report(book, fy_of(today), kinds.get).indian
    plan = tax_planning(book, prices, today, kinds.get)
    stcg = max(0.0, rep_.net_stcg) if rep_ else 0.0
    ltcg_taxable = max(0.0, rep_.net_ltcg - rep_.exemption_limit) if rep_ else 0.0
    return {"fy": plan.fy, **wealth.harvest_plan(book, prices, today, plan.exemption_remaining, stcg, ltcg_taxable, kinds.get)}


class HarvestRow(BaseModel):
    symbol: str = Field(max_length=40)
    quantity: float = Field(gt=0)
    price: float = Field(gt=0)


class HarvestBody(BaseModel):
    items: list[HarvestRow] = Field(min_length=1, max_length=40)
    rebuy: bool = True
    trade_date: dt.date | None = None


@router.post("/harvest/record")
async def record_harvest(body: HarvestBody, store: Store = Depends(deps.get_store)) -> dict[str, Any]:
    """Record harvesting trades you've placed at your broker: a sale and (by default) the same-day buy-back."""
    day = body.trade_date or dt.date.today()
    book = build_book(_transactions(store))
    rows = []
    for it in body.items:
        held = sum(l.quantity for l in book.symbols[it.symbol].lots) if it.symbol in book.symbols else 0.0
        if it.quantity > held + 1e-6:
            raise HTTPException(status_code=422, detail=f"You hold {held:g} of {it.symbol}, not {it.quantity:g}")
        rows.append({"symbol": it.symbol, "side": "sell", "quantity": it.quantity, "price": it.price, "trade_date": day, "note": "Tax harvesting: sale"})
        if body.rebuy:
            rows.append({"symbol": it.symbol, "side": "buy", "quantity": it.quantity, "price": it.price, "trade_date": day, "note": "Tax harvesting: buy-back"})
    valid = [TransactionIn(**r) for r in rows]
    ids = store.add_transactions([v.model_dump() for v in valid], source="harvest")
    return {"recorded": len(ids)}


@router.get("/direct-savings")
async def direct_savings(store: Store = Depends(deps.get_store), provider: MarketDataProvider = Depends(get_live_provider)) -> dict[str, Any]:
    """For each Regular-plan fund you hold: the Direct plan of the same fund, how much better it has done, and what staying costs."""
    funds = getattr(provider, "funds", None)
    if funds is None:
        raise HTTPException(status_code=503, detail="mutual-fund data is not configured")
    today = dt.date.today()
    book = build_book(_transactions(store))
    try:
        catalog = await run_in_threadpool(funds.data.catalog)
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail=f"AMFI scheme list unavailable: {exc}") from None
    schemes = list(catalog.values())
    items, total_value, total_future = [], 0.0, 0.0
    for sym, b in book.symbols.items():
        if not is_mf_symbol(sym) or not b.lots:
            continue
        s = catalog.get(mf_code(sym))
        if s is None or (s.plan or "") != "Regular":
            continue
        d = wealth.plan_sibling(s, schemes, "Direct")
        units = sum(l.quantity for l in b.lots)
        value = units * (s.nav or 0)
        held = (today - b.lots[0].date).days / 365.25
        row = {"symbol": sym, "name": s.name, "option": s.option, "units": round(units, 3), "value": round(value, 2),
               "held_years": round(held, 1), "direct": {"symbol": f"MF{d.code}", "code": d.code, "nav": d.nav} if d else None}
        if d:
            try:
                reg_s, dir_s = await asyncio.gather(run_in_threadpool(funds.data.history, s.code), run_in_threadpool(funds.data.history, d.code))
                gap = wealth.annual_gap(reg_s, dir_s)
            except ProviderError:
                gap = None
            row["gap"] = gap
            if gap and gap["gap"] > 0:
                row.update(wealth.switch_value(value, gap["gap"], held))
                total_value += value; total_future += row["next_years_cost"]
        items.append(row)
    return {"items": items, "regular_value": round(total_value, 2), "next_years_cost": round(total_future, 2),
            "note": ("The gap is the Direct plan's extra yearly return over the same period, mostly the distributor commission. "
                     "Switching is a sale and a fresh purchase: it can trigger capital-gains tax and exit loads, so time it (e.g. after units turn long-term) "
                     "and check with your distributor or adviser. Projections assume 12% a year and are illustrations, not promises.")}


@router.get("/health")
async def health(currency: str = Query(BASE_CURRENCY, pattern=r"^[A-Z]{3}$"), store: Store = Depends(deps.get_store),
                 provider: MarketDataProvider = Depends(get_live_provider)) -> dict[str, Any]:
    book = build_book(_transactions(store))
    quotes, _ = await deps.quotes_for(book.open_symbols(), provider)
    view = value_portfolio(book, quotes, today=dt.date.today())
    positions = [p for p in view.positions if p.currency == currency and p.value]
    if not positions:
        raise HTTPException(status_code=404, detail=f"no valued {currency} holdings to check")
    syms = [p.symbol for p in positions]
    hist = await deps.gather_limited(syms, lambda s: deps.history(s, "1y", provider))
    prof = await deps.gather_limited(syms, lambda s: deps.profile(s, provider))
    histories = {s: h.bars for s, h in zip(syms, hist) if not isinstance(h, BaseException)}
    sectors = {s: p.sector for s, p in zip(syms, prof) if not isinstance(p, BaseException)}
    bench = None
    if currency in BENCHMARKS:
        label, sym = BENCHMARKS[currency]
        try:
            bench = (label, (await deps.history(sym, "1y", provider)).bars)
        except ToolError:
            bench = None
    report = build_health(currency, [HoldingInput(symbol=p.symbol, value=p.value, pnl_pct=p.pnl_pct,
                                                  sector=sectors.get(p.symbol)) for p in positions],
                          histories, bench)
    return report.model_dump(mode="json")
