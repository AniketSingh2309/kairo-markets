"""CAS import: a CAMS / KFintech Consolidated Account Statement (PDF) -> mutual-fund transactions.

The detailed CAS lists, per folio and scheme, an opening unit balance, every transaction (date, description,
amount, units, NAV, running unit balance) and a closing line with the closing balance, NAV and total cost.
Each scheme is matched to an AMFI scheme code by ISIN (falling back to its name), and every purchase,
SIP, switch-in, redemption and switch-out becomes a buy or sell at that day's NAV. Stamp duty, STT and
other one-number lines carry no units and are skipped.

Each scheme is checked: the units read must add up to the statement's own closing balance. A scheme that
doesn't reconcile is reported rather than imported, so a parsing slip can never corrupt the portfolio.
If the statement starts mid-history (non-zero opening balance), that balance becomes one opening lot whose
cost is backed out from the statement's total cost value, and is marked as estimated.
"""

from __future__ import annotations

import datetime as dt
import difflib
import io
import re
from dataclasses import dataclass, field
from typing import Any

DATE = r"\d{2}-[A-Za-z]{3}-\d{4}"
NUM = r"\(?-?[\d,]+\.\d+\)?"
_TXN = re.compile(rf"^({DATE})\s+(.+?)\s+({NUM})\s+({NUM})\s+({NUM})\s+({NUM})\s*$")
_ISIN = re.compile(r"\b(IN[FE][A-Z0-9]{9})\b")
_FOLIO = re.compile(r"Folio\s*No\s*:?\s*([\w/ ]+?)(?:\s{2,}|\s+PAN|\s+KYC|$)", re.I)
_OPEN = re.compile(rf"Opening\s+Unit\s+Balance\s*:?\s*({NUM})", re.I)
_CLOSE = re.compile(rf"Closing\s+Unit\s+Balance\s*:?\s*({NUM})", re.I)
_COST = re.compile(rf"(?:Total\s+)?Cost\s+Value\s*:?\s*(?:INR|Rs\.?|₹)?\s*({NUM})", re.I)
_NAV = re.compile(rf"NAV\s+on\s+({DATE})\s*:?\s*(?:INR|Rs\.?|₹)?\s*({NUM})", re.I)


class CasError(ValueError):
    pass


def _n(v: str) -> float:
    neg = v.startswith("(") and v.endswith(")") or v.startswith("-")
    x = float(v.strip("()").replace(",", "").lstrip("-"))
    return -x if neg else x


def _d(v: str) -> dt.date:
    return dt.datetime.strptime(v, "%d-%b-%Y").date()


@dataclass
class CasTxn:
    date: dt.date
    description: str
    amount: float
    units: float
    nav: float
    balance: float


@dataclass
class CasScheme:
    name: str
    isin: str | None
    folio: str | None
    opening: float = 0.0
    closing: float | None = None
    nav: float | None = None
    nav_date: dt.date | None = None
    cost: float | None = None
    txns: list[CasTxn] = field(default_factory=list)

    @property
    def units_read(self) -> float:
        return round(self.opening + sum(t.units for t in self.txns), 3)

    @property
    def reconciles(self) -> bool:
        return self.closing is not None and abs(self.units_read - self.closing) <= 0.002 + 1e-6 * abs(self.closing)


def read_pdf_text(data: bytes, password: str = "") -> str:
    """Text of a (possibly password-protected) CAS PDF."""
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        raise CasError("PDF support isn't installed (pip install pypdf cryptography)") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(password or ""):
            raise CasError("That password didn't open the PDF. CAS files from CAMS / KFintech usually use your PAN in capitals (or the password you set when requesting it).")
        pages = [p.extract_text() or "" for p in reader.pages]
    except CasError:
        raise
    except (PdfReadError, ValueError, KeyError) as exc:
        raise CasError(f"Couldn't read that PDF: {exc}") from exc
    text = "\n".join(pages)
    if not text.strip():
        raise CasError("The PDF has no text layer (is it a scanned image?). Download the CAS again from CAMS or KFintech.")
    return text


def parse_cas(text: str) -> list[CasScheme]:
    schemes: list[CasScheme] = []
    folio, cur = None, None
    pending_name = ""
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line:
            continue
        if m := _FOLIO.search(line):
            folio = re.sub(r"\s+", " ", m.group(1)).strip(" /")
            continue
        isin = _ISIN.search(line)
        if isin and not _TXN.match(line):
            name = re.split(r"\s*[-–]?\s*ISIN\b", (pending_name + " " + line).strip(), maxsplit=1)[0]
            name = re.sub(r"^[A-Z0-9]{2,8}\s*-\s*", "", name).strip(" -")     # scheme code prefix like "128TSDGG-"
            name = re.sub(r"\(?(Advisor|Registrar)\b.*$", "", name, flags=re.I).strip(" -(")
            cur = CasScheme(name=name or isin.group(1), isin=isin.group(1), folio=folio)
            schemes.append(cur); pending_name = ""
            continue
        if cur is None:
            pending_name = line if len(line) < 160 and not re.search(DATE, line) else ""
            continue
        if m := _OPEN.search(line):
            cur.opening = _n(m.group(1)); continue
        if m := _TXN.match(line):
            amount, units, nav, bal = (_n(m.group(i)) for i in (3, 4, 5, 6))
            if units != 0 and nav > 0:
                cur.txns.append(CasTxn(_d(m.group(1)), m.group(2).strip(), amount, units, nav, bal))
            continue
        if m := _CLOSE.search(line):
            cur.closing = _n(m.group(1))
            if n := _NAV.search(line):
                cur.nav_date, cur.nav = _d(n.group(1)), _n(n.group(2))
            if c := _COST.search(line):
                cur.cost = _n(c.group(1))
            continue
        if cur.closing is not None:
            if (n := _NAV.search(line)) and cur.nav is None:
                cur.nav_date, cur.nav = _d(n.group(1)), _n(n.group(2))
            if (c := _COST.search(line)) and cur.cost is None:
                cur.cost = _n(c.group(1))
            pending_name = line if len(line) < 160 and not re.search(DATE, line) else pending_name
    if not schemes:
        raise CasError("No mutual-fund schemes found. Is this a detailed CAS from CAMS or KFintech (not a summary or a demat CAS)?")
    return schemes


def match_scheme(s: CasScheme, catalog: dict[str, Any]) -> tuple[str | None, str]:
    """(AMFI scheme code, how it was matched) for a CAS scheme."""
    if s.isin:
        for code, sc in catalog.items():
            if getattr(sc, "isin", None) == s.isin:
                return code, "isin"
    names = {sc.name.lower(): code for code, sc in catalog.items()}
    best = difflib.get_close_matches(s.name.lower(), list(names), n=1, cutoff=0.82)
    return (names[best[0]], "name") if best else (None, "none")


def to_transactions(schemes: list[CasScheme], catalog: dict[str, Any]) -> dict[str, Any]:
    """Preview: transaction rows for the portfolio, a per-scheme summary, and errors."""
    rows, summary, errors = [], [], []
    for s in schemes:
        code, how = match_scheme(s, catalog)
        sym = f"MF{code}" if code else None
        info = {"name": s.name, "isin": s.isin, "folio": s.folio, "matched": catalog[code].name if code else None, "match": how,
                "symbol": sym, "transactions": len(s.txns), "units_read": s.units_read, "closing_units": s.closing,
                "reconciles": s.reconciles, "estimated_opening": False, "imported": False}
        summary.append(info)
        if not code:
            errors.append(f"{s.name}: no matching AMFI scheme (ISIN {s.isin or 'missing'}); skipped.")
            continue
        if s.closing is None:
            errors.append(f"{s.name}: no closing balance found; skipped so nothing is imported half-read.")
            continue
        if not s.reconciles:
            errors.append(f"{s.name}: units read ({s.units_read:g}) don't add up to the closing balance ({s.closing:g}); skipped.")
            continue
        if s.opening > 0.0005:
            buys = sum(t.amount for t in s.txns if t.units > 0)
            sold_units = -sum(t.units for t in s.txns if t.units < 0)
            first = min((t.date for t in s.txns), default=s.nav_date or dt.date.today())
            est = None
            if s.cost is not None and sold_units < 0.001:
                est = (s.cost - buys) / s.opening
            price = est if est and est > 0 else (s.txns[0].nav if s.txns else s.nav)
            if price:
                rows.append({"symbol": sym, "side": "buy", "quantity": round(s.opening, 4), "price": round(price, 4), "fees": 0.0,
                             "trade_date": (first - dt.timedelta(days=1)).isoformat(), "note": "CAS opening balance (cost estimated)"})
                info["estimated_opening"] = True
        for t in s.txns:
            rows.append({"symbol": sym, "side": "buy" if t.units > 0 else "sell", "quantity": round(abs(t.units), 4), "price": round(t.nav, 4),
                         "fees": 0.0, "trade_date": t.date.isoformat(), "note": f"CAS: {t.description[:150]}"})
        info["imported"] = True
    return {"rows": rows, "schemes": summary, "errors": errors}
