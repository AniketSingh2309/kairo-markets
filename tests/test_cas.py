"""CAS (consolidated account statement) import: PDF decryption, parsing, reconciliation, AMFI matching."""

import datetime as dt
import io
from types import SimpleNamespace

import pytest

from core import cas

SAMPLE = """Consolidated Account Statement
01-Jan-2022 To 30-Sep-2026
Folio No: 12345678 / 90   PAN: ABCDE1234F   KYC: OK
128EFDGG-Axis Bluechip Fund - Direct Growth - ISIN: INF846K01DP8(Advisor: DIRECT) Registrar : KFINTECH
Opening Unit Balance: 0.000
10-Jan-2022 Purchase - Via Internet 5,000.00 112.360 44.5000 112.360
*** Stamp Duty *** 0.25
10-Feb-2022 SIP Purchase - Instalment No 2 5,000.00 110.000 45.4545 222.360
15-Mar-2024 Redemption - Via Internet (2,500.00) (50.000) 50.0000 172.360
Closing Unit Balance: 172.360 NAV on 30-Sep-2026: INR 55.23 Total Cost Value: 7,750.00 Market Value on 30-Sep-2026: INR 9,519.44
Folio No: 99887766   PAN: ABCDE1234F
B205RG-Parag Parikh Flexi Cap Fund - Regular Plan Growth - ISIN: INF879O01019(Advisor: ARN-0000)
Opening Unit Balance: 100.000
05-Jun-2023 Systematic Investment 10,000.00 200.000 50.0000 300.000
Closing Unit Balance: 300.000 NAV on 30-Sep-2026: INR 80.10 Total Cost Value: 13,000.00
Folio No: 55555
X1-Mystery Fund Growth - ISIN: INF000X00000
Opening Unit Balance: 0.000
01-Feb-2025 Purchase 1,000.00 10.000 100.0000 10.000
Closing Unit Balance: 12.000 NAV on 30-Sep-2026: INR 101.00
"""

CATALOG = {
    "120465": SimpleNamespace(name="Axis Bluechip Fund - Direct Plan - Growth", isin="INF846K01DP8"),
    "122639": SimpleNamespace(name="Parag Parikh Flexi Cap Fund - Regular Plan - Growth", isin="INF879O01019"),
}


def test_parse_sample():
    s = cas.parse_cas(SAMPLE)
    assert [x.isin for x in s] == ["INF846K01DP8", "INF879O01019", "INF000X00000"]
    axis = s[0]
    assert axis.name.startswith("Axis Bluechip Fund") and axis.folio == "12345678 / 90"
    assert len(axis.txns) == 3 and axis.txns[2].units == -50 and axis.txns[2].amount == -2500
    assert axis.closing == 172.36 and axis.nav == 55.23 and axis.cost == 7750 and axis.reconciles
    assert s[1].opening == 100 and s[1].reconciles and not s[2].reconciles


def test_to_transactions_reconciles_and_estimates_opening():
    out = cas.to_transactions(cas.parse_cas(SAMPLE), CATALOG)
    axis = [r for r in out["rows"] if r["symbol"] == "MF120465"]
    assert [(r["side"], r["quantity"], r["price"]) for r in axis] == [("buy", 112.36, 44.5), ("buy", 110.0, 45.4545), ("sell", 50.0, 50.0)]
    ppfas = [r for r in out["rows"] if r["symbol"] == "MF122639"]
    assert ppfas[0]["note"].startswith("CAS opening balance") and ppfas[0]["price"] == 30.0     # (13,000 - 10,000) / 100 units
    assert ppfas[0]["trade_date"] == "2023-06-04"
    assert {x["name"].split(" ")[0]: x["imported"] for x in out["schemes"]} == {"Axis": True, "Parag": True, "Mystery": False}
    assert any("Mystery" in e for e in out["errors"])


def _pdf(lines, password):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    w = PdfWriter()
    page = w.add_blank_page(612, 792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): w._add_object(font)})})
    esc = lambda t: t.replace("\\", "\\\\").replace("(", "\(").replace(")", "\)")  # noqa: E731
    body = "BT /F1 8 Tf 20 770 Td 10 TL " + " ".join(f"({esc(t)}) Tj T*" for t in lines) + " ET"
    stream = DecodedStreamObject(); stream.set_data(body.encode("latin-1"))
    page[NameObject("/Contents")] = w._add_object(stream)
    w.encrypt(user_password=password, owner_password=None, algorithm="AES-256")
    buf = io.BytesIO(); w.write(buf)
    return buf.getvalue()


def test_encrypted_pdf_round_trip():
    data = _pdf(SAMPLE.strip().splitlines(), "ABCDE1234F")
    with pytest.raises(cas.CasError, match="password"):
        cas.read_pdf_text(data, "WRONG")
    schemes = cas.parse_cas(cas.read_pdf_text(data, "ABCDE1234F"))
    assert len(schemes) == 3 and schemes[0].reconciles and schemes[1].reconciles


def test_not_a_cas():
    with pytest.raises(cas.CasError, match="No mutual-fund schemes"):
        cas.parse_cas("Some random statement\nwith no schemes")


def test_import_cas_api_preview_import_and_dedupe():
    import base64
    from fastapi.testclient import TestClient
    from api import deps
    from api.live import get_live_provider
    from api.main import app
    from core.store import Store
    store = Store(":memory:")
    provider = SimpleNamespace(funds=SimpleNamespace(data=SimpleNamespace(catalog=lambda: CATALOG)))
    app.dependency_overrides[deps.get_store] = lambda: store
    app.dependency_overrides[get_live_provider] = lambda: provider
    body = {"pdf_base64": base64.b64encode(_pdf(SAMPLE.strip().splitlines(), "ABCDE1234F")).decode(), "password": "abcde1234f ".upper()}
    try:
        c = TestClient(app)
        prev = c.post("/portfolio/import-cas", json=body).json()
        assert prev["imported"] == 0 and len(prev["rows"]) == 5 and store.list_transactions() == []
        done = c.post("/portfolio/import-cas", json={**body, "dry_run": False}).json()
        assert done["imported"] == 5 and {t["source"] for t in store.list_transactions()} == {"import:cas"}
        again = c.post("/portfolio/import-cas", json={**body, "dry_run": False}).json()
        assert again["imported"] == 0 and again["already_imported"] == 5
        bad = c.post("/portfolio/import-cas", json={**body, "password": "nope"})
        assert bad.status_code == 422 and "password" in bad.json()["detail"]
    finally:
        app.dependency_overrides.clear()
