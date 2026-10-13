"""Accounts: sign-up / log-in, the 4-digit app PIN lock, per-user data isolation, reset, export and deletion."""

import datetime as dt
import sqlite3

import pytest
from fastapi.testclient import TestClient

from api import auth as auth_api
from api import deps
from core.accounts import AccountError, Accounts, check_pin, check_secret, hash_secret
from core.store import Store

GOOD = {"name": "Asha", "email": "asha@example.com", "password": "trade2win", "age_confirmed": True, "terms_accepted": True}


@pytest.fixture
def env(tmp_path, monkeypatch):
    acc = Accounts(tmp_path / "accounts.db")
    legacy = tmp_path / "kairo.db"
    monkeypatch.setattr(deps, "auth_on", lambda: True)
    monkeypatch.setattr(deps, "get_accounts", lambda: acc)
    monkeypatch.setattr(deps, "user_db_path", lambda uid: tmp_path / "users" / f"{uid}.db")
    monkeypatch.setattr(auth_api, "get_settings", lambda: type("S", (), {"db_path": legacy, "cookie_secure": False, "smtp_host": None})())
    deps._USER_STORES.clear()
    from api.main import app
    yield TestClient(app), acc, tmp_path, legacy
    for st in list(deps._USER_STORES.values()):
        st._conn.close()
    deps._USER_STORES.clear()


def test_hashing_and_validation():
    h = hash_secret("2580")
    assert h.startswith("scrypt$") and check_secret("2580", h) and not check_secret("2581", h) and h != hash_secret("2580")
    acc = Accounts(":memory:")
    for bad, msg in [({"email": "nope"}, "valid email"), ({"password": "short1"}, "8 characters"), ({"password": "longbutnodigits"}, "number"),
                     ({"age_confirmed": False}, "18"), ({"terms_accepted": False}, "Terms"), ({"name": " "}, "name")]:
        args = {**GOOD, **bad}
        with pytest.raises(AccountError, match=msg):
            acc.signup(args["email"], args["password"], args["name"], args["age_confirmed"], args["terms_accepted"])
    with pytest.raises(AccountError, match="too easy"):
        check_pin("1234")


def test_signup_login_and_isolation(env):
    c, acc, tmp, _ = env
    assert c.get("/auth/me").json() == {"auth": "on", "user": None, "locked": False, "first_account": True}
    assert c.get("/portfolio/transactions").status_code == 401                      # personal data needs a log-in
    r = c.post("/auth/signup", json={**GOOD, "terms_accepted": False})
    assert r.status_code == 422 and "Terms" in r.json()["detail"]["message"]
    r = c.post("/auth/signup", json=GOOD)
    assert r.status_code == 200 and r.json()["user"]["email"] == "asha@example.com" and not r.json()["user"]["pin_set"]
    assert "kairo_session" in c.cookies
    assert c.post("/auth/signup", json=GOOD).status_code == 409                       # email taken
    c.post("/portfolio/transactions", json={"symbol": "MF120465", "side": "buy", "quantity": 1, "price": 10, "trade_date": "2025-01-01"})
    assert len(c.get("/portfolio/transactions").json()) == 1
    # a second person sees only their own (empty) data
    b = TestClient(c.app)
    b.post("/auth/signup", json={**GOOD, "email": "ravi@example.com", "name": "Ravi"})
    assert b.get("/portfolio/transactions").json() == []
    assert (tmp / "users").exists() and len(list((tmp / "users").glob("*.db"))) == 2
    # log out, wrong password, log in again
    c.post("/auth/logout")
    assert c.get("/portfolio/transactions").status_code == 401
    assert c.post("/auth/login", json={"email": "ASHA@example.com ", "password": "wrong1234"}).status_code == 401
    assert c.post("/auth/login", json={"email": "asha@example.com", "password": "trade2win"}).status_code == 200
    assert len(c.get("/portfolio/transactions").json()) == 1


def test_pin_lock_flow(env):
    c, acc, _, _ = env
    c.post("/auth/signup", json=GOOD)
    assert c.post("/auth/pin", json={"pin": "1234"}).status_code == 422                # too easy
    r = c.post("/auth/pin", json={"pin": "2580"})
    assert r.status_code == 200 and r.json()["user"]["pin_set"] and not r.json()["locked"]
    assert c.get("/auth/me", params={"app_open": True}).json()["locked"] is False     # reopened moments later: no PIN
    c.post("/auth/lock")
    assert c.get("/portfolio/transactions").status_code == 423                        # the server enforces the lock
    assert c.get("/auth/me").json()["locked"] is True
    r = c.post("/auth/unlock", json={"pin": "1111"})
    assert r.status_code == 401 and "4 tries left" in r.json()["detail"]["message"]
    assert c.post("/auth/unlock", json={"pin": "2580"}).status_code == 200
    assert c.get("/portfolio/transactions").status_code == 200
    # changing the PIN needs the password
    assert c.post("/auth/pin", json={"pin": "3690"}).status_code == 401
    assert c.post("/auth/pin", json={"pin": "3690", "password": "trade2win"}).status_code == 200
    # idle: the unlock window ran out
    with acc._tx() as con:
        con.execute("UPDATE sessions SET unlocked_until=?", ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)).isoformat(),))
    assert c.get("/portfolio/transactions").status_code == 423
    # five wrong PINs end the session
    for _ in range(4):
        c.post("/auth/unlock", json={"pin": "0001"})
    r = c.post("/auth/unlock", json={"pin": "0001"})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "PIN_LOCKOUT"
    assert c.get("/auth/me").json()["user"] is None
    assert c.post("/auth/login", json={"email": GOOD["email"], "password": GOOD["password"]}).json()["locked"] is False
    assert c.post("/auth/pin/remove", json={"password": "trade2win"}).json()["user"]["pin_set"] is False


def test_login_pause_after_repeated_failures(env):
    c, acc, _, _ = env
    c.post("/auth/signup", json=GOOD)
    for _ in range(5):
        c.post("/auth/login", json={"email": GOOD["email"], "password": "nopenope1"})
    r = c.post("/auth/login", json={"email": GOOD["email"], "password": GOOD["password"]})
    assert r.status_code == 429 and "minute" in r.json()["detail"]["message"]


def test_onboarding_profile_and_password_reset(env):
    c, acc, _, _ = env
    c.post("/auth/signup", json=GOOD)
    r = c.post("/auth/onboarding", json={"experience": "new", "interests": ["stocks", "fno"], "language": "hi", "paper_capital": 200000})
    u = r.json()["user"]
    assert u["onboarded"] and u["profile"]["interests"] == ["stocks", "fno"]
    assert c.get("/trading/funds").json()["start_cash"] == 200000                        # paper account sized from onboarding
    assert c.patch("/auth/profile", json={"name": "Asha K", "whatsapp_opt_in": True}).json()["user"]["whatsapp_opt_in"]
    assert c.post("/auth/password/forgot", json={"email": "nobody@example.com"}).json()["sent"]   # no account probing
    _, code = acc.start_reset(GOOD["email"])
    other = TestClient(c.app)
    assert other.post("/auth/password/reset", json={"email": GOOD["email"], "code": "000000" if code != "000000" else "111111", "new_password": "newpass99"}).status_code == 400
    assert other.post("/auth/password/reset", json={"email": GOOD["email"], "code": code, "new_password": "newpass99"}).status_code == 200
    assert c.get("/portfolio/transactions").status_code == 401                         # the reset logged out other devices
    assert other.get("/portfolio/transactions").status_code == 200


def test_export_and_delete(env):
    c, acc, tmp, _ = env
    c.post("/auth/signup", json=GOOD)
    uid = c.get("/auth/me").json()["user"]["id"]
    c.post("/portfolio/transactions", json={"symbol": "MF120465", "side": "buy", "quantity": 2, "price": 10, "trade_date": "2025-01-01"})
    ex = c.get("/auth/export").json()
    assert ex["account"]["email"] == GOOD["email"] and len(ex["data"]["transactions"]) == 1 and "password_hash" not in str(ex)
    assert c.post("/auth/delete", json={"password": "trade2win", "confirm": "nope"}).status_code == 422
    assert c.post("/auth/delete", json={"password": "wrong", "confirm": "DELETE"}).status_code == 401
    assert c.post("/auth/delete", json={"password": "trade2win", "confirm": "DELETE"}).json()["deleted"]
    assert not (tmp / "users" / f"{uid}.db").exists() and acc.user(uid) is None
    assert c.post("/auth/login", json={"email": GOOD["email"], "password": "trade2win"}).status_code == 401


def test_first_account_adopts_existing_single_user_data(env):
    c, acc, tmp, legacy = env
    old = Store(legacy)
    old.add_transactions([{"symbol": "TCS.NS", "side": "buy", "quantity": 3, "price": 100, "trade_date": "2025-01-01"}])
    old._conn.close()
    r = c.post("/auth/signup", json=GOOD).json()
    assert r["adopted_existing_data"] is True
    assert [t["symbol"] for t in c.get("/portfolio/transactions").json()] == ["TCS.NS"]
    second = TestClient(c.app)
    assert second.post("/auth/signup", json={**GOOD, "email": "b@example.com"}).json()["adopted_existing_data"] is False
    assert second.get("/portfolio/transactions").json() == []
    assert sqlite3.connect(str(legacy)).execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1   # the original is untouched
