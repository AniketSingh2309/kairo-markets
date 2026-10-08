"""Portfolio API: transactions, live valuation with XIRR, tax P&L, health check, CSV import."""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from api import deps
from api.live import get_live_provider
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
