"""Account API: sign-up, log-in, the 4-digit app PIN, onboarding, profile, password reset, export and deletion.

The session lives in an httpOnly cookie. Every endpoint with personal data goes through ``deps.get_store``,
which answers 401 when signed out and 423 when the app PIN is needed, so the lock is enforced by the server,
not just hidden in the browser.
"""

from __future__ import annotations

import datetime as dt
import logging
import smtplib
import sqlite3
import time
from email.message import EmailMessage
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from api import deps
from core.accounts import SESSION_DAYS, AccountError, Accounts, public_user
from core.config import get_settings

log = logging.getLogger("kairo.auth")
router = APIRouter(prefix="/auth", tags=["accounts"])
PERSONAL_TABLES = ("transactions", "alerts", "paper_orders", "paper_fills", "paper_gtt", "paper_super", "baskets", "scans", "journal_notes")
_RESET_SENT: dict[str, float] = {}


def _fail(exc: AccountError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail={"code": exc.code, "message": str(exc)})


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(deps.AUTH_COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        secure=get_settings().cookie_secure, path="/")


def _session_or_401(request: Request) -> tuple[str, dict[str, Any]]:
    """The session for this request even if PIN-locked (the PIN endpoints need that)."""
    token = request.cookies.get(deps.AUTH_COOKIE)
    s = deps.get_accounts().session(token)
    if s is None:
        raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED", "message": "Please log in to continue."})
    return token, s


def _unlocked(request: Request) -> tuple[str, dict[str, Any]]:
    token, s = _session_or_401(request)
    if deps.get_accounts().is_locked(s):
        raise HTTPException(status_code=423, detail={"code": "LOCKED", "message": "Enter your PIN to continue."})
    return token, s


def _me(accounts: Accounts, user_id: str, locked: bool = False) -> dict[str, Any]:
    return {"auth": "on", "user": public_user(accounts.user(user_id)), "locked": locked}


def _adopt_existing_data(user_id: str) -> bool:
    """The very first account takes over the data Kairo already had from single-user mode."""
    src = get_settings().db_path
    if not src.exists():
        return False
    con = sqlite3.connect(str(src))
    try:
        has = any(con.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone() for t in PERSONAL_TABLES
                  if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone())
        if not has:
            return False
        dst_path = deps.user_db_path(user_id)
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        dst = sqlite3.connect(str(dst_path))
        con.backup(dst)  # a consistent copy even while the server is running
        dst.close()
        return True
    finally:
        con.close()


# ------------------------------------------------------------------------------------------ status


@router.get("/me")
async def me(request: Request, app_open: bool = False) -> dict[str, Any]:
    """Who is signed in. ``app_open`` (page load): a session with a PIN is locked unless used moments ago."""
    if not deps.auth_on():
        return {"auth": "off", "user": None, "locked": False}
    accounts, token = deps.get_accounts(), request.cookies.get(deps.AUTH_COOKIE)
    s = accounts.session(token)
    if s is None:
        return {"auth": "on", "user": None, "locked": False, "first_account": accounts.count() == 0}
    if app_open:
        accounts.app_opened(token, s)
        s = accounts.session(token)
    locked = accounts.is_locked(s)
    if not locked:
        accounts.touch(token, s)
    return _me(accounts, s["user_id"], locked)


# ------------------------------------------------------------------------------------------ sign-up / log-in


class SignupIn(BaseModel):
    name: str = Field(max_length=60)
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)
    age_confirmed: bool = False
    terms_accepted: bool = False
    whatsapp_opt_in: bool = False


@router.post("/signup")
async def signup(body: SignupIn, request: Request, response: Response) -> dict[str, Any]:
    accounts = deps.get_accounts()
    first = accounts.count() == 0
    try:
        u = await run_in_threadpool(accounts.signup, body.email, body.password, body.name, body.age_confirmed, body.terms_accepted, body.whatsapp_opt_in)
    except AccountError as exc:
        raise _fail(exc) from None
    adopted = first and await run_in_threadpool(_adopt_existing_data, u["id"])
    _set_cookie(response, accounts.create_session(u["id"], request.headers.get("user-agent", "")))
    return {**_me(accounts, u["id"]), "adopted_existing_data": adopted}


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response) -> dict[str, Any]:
    accounts = deps.get_accounts()
    try:
        u = await run_in_threadpool(accounts.authenticate, body.email, body.password)
    except AccountError as exc:
        raise _fail(exc) from None
    _set_cookie(response, accounts.create_session(u["id"], request.headers.get("user-agent", "")))
    return _me(accounts, u["id"])


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict[str, Any]:
    token = request.cookies.get(deps.AUTH_COOKIE)
    if token:
        deps.get_accounts().end_session(token)
    response.delete_cookie(deps.AUTH_COOKIE, path="/")
    return {"logged_out": True}


# ------------------------------------------------------------------------------------------ app PIN


class PinIn(BaseModel):
    pin: str = Field(min_length=4, max_length=4)
    password: str | None = Field(None, max_length=128)


@router.post("/pin")
async def set_pin(body: PinIn, request: Request) -> dict[str, Any]:
    """Set or change the PIN. Changing an existing PIN needs the account password."""
    token, s = _unlocked(request)
    accounts = deps.get_accounts()
    try:
        if accounts.user(s["user_id"])["pin_hash"]:
            await run_in_threadpool(accounts.verify_password, s["user_id"], body.password or "")
        await run_in_threadpool(accounts.set_pin, s["user_id"], body.pin)
    except AccountError as exc:
        raise _fail(exc) from None
    accounts.unlock(token, accounts.session(token), body.pin)  # this session stays open
    return _me(accounts, s["user_id"])


class PinCheck(BaseModel):
    pin: str = Field(min_length=1, max_length=8)


@router.post("/unlock")
async def unlock(body: PinCheck, request: Request, response: Response) -> dict[str, Any]:
    token, s = _session_or_401(request)
    accounts = deps.get_accounts()
    try:
        await run_in_threadpool(accounts.unlock, token, s, body.pin)
    except AccountError as exc:
        if exc.code == "PIN_LOCKOUT":
            response.delete_cookie(deps.AUTH_COOKIE, path="/")
        raise _fail(exc) from None
    return _me(accounts, s["user_id"])


@router.post("/lock")
async def lock(request: Request) -> dict[str, Any]:
    token, s = _session_or_401(request)
    deps.get_accounts().lock(token)
    return {"locked": bool(s.get("pin_hash"))}


class PasswordIn(BaseModel):
    password: str = Field(max_length=128)


@router.post("/pin/remove")
async def remove_pin(body: PasswordIn, request: Request) -> dict[str, Any]:
    _, s = _unlocked(request)
    accounts = deps.get_accounts()
    try:
        await run_in_threadpool(accounts.verify_password, s["user_id"], body.password)
    except AccountError as exc:
        raise _fail(exc) from None
    accounts.remove_pin(s["user_id"])
    return _me(accounts, s["user_id"])


# ------------------------------------------------------------------------------------------ profile


class OnboardingIn(BaseModel):
    experience: Literal["new", "under_1y", "1_3y", "3y_plus"] | None = None
    interests: list[Literal["stocks", "fno", "mutual_funds", "long_term"]] = Field(default_factory=list, max_length=4)
    style: Literal["intraday", "swing", "long_term", "not_sure"] | None = None
    language: Literal["en", "hi"] | None = None
    paper_capital: int | None = Field(None, ge=10_000, le=100_000_000)
    skipped: bool = False


@router.post("/onboarding")
async def onboarding(body: OnboardingIn, request: Request) -> dict[str, Any]:
    _, s = _unlocked(request)
    accounts = deps.get_accounts()
    profile = body.model_dump(exclude_none=True)
    if not profile.get("interests"):
        profile.pop("interests", None)
    accounts.update_profile(s["user_id"], **profile)
    if body.paper_capital:
        store = deps.user_store(s["user_id"])
        if not store.paper_fills():  # only before the first paper trade
            store.set_setting("paper_start_cash", str(body.paper_capital))
    return _me(accounts, s["user_id"])


class ProfileIn(BaseModel):
    name: str | None = Field(None, max_length=60)
    whatsapp_opt_in: bool | None = None


@router.patch("/profile")
async def profile(body: ProfileIn, request: Request) -> dict[str, Any]:
    _, s = _unlocked(request)
    accounts = deps.get_accounts()
    try:
        if body.name is not None:
            accounts.set_name(s["user_id"], body.name)
        if body.whatsapp_opt_in is not None:
            accounts.set_whatsapp(s["user_id"], body.whatsapp_opt_in)
    except AccountError as exc:
        raise _fail(exc) from None
    return _me(accounts, s["user_id"])


class PasswordChange(BaseModel):
    current: str = Field(max_length=128)
    new: str = Field(max_length=128)


@router.post("/password")
async def change_password(body: PasswordChange, request: Request) -> dict[str, Any]:
    token, s = _unlocked(request)
    accounts = deps.get_accounts()
    try:
        await run_in_threadpool(accounts.change_password, s["user_id"], body.current, body.new)
    except AccountError as exc:
        raise _fail(exc) from None
    accounts.end_other_sessions(s["user_id"], token)  # other devices must log in again
    return {"changed": True}


# ------------------------------------------------------------------------------------------ password reset


def _send_code(email: str, name: str, code: str) -> bool:
    st = get_settings()
    if not st.smtp_host:
        log.warning("Password reset code for %s: %s (set SMTP_HOST to email it instead)", email, code)
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = "Your Kairo password reset code", st.smtp_from or st.smtp_user, email
    msg.set_content(f"Hi {name},\n\nYour Kairo password reset code is {code}. It expires in 15 minutes.\n\n"
                    "If you didn't ask for this, you can ignore this email; your password hasn't changed.\n")
    with smtplib.SMTP(st.smtp_host, st.smtp_port, timeout=15) as smtp:
        smtp.starttls()
        if st.smtp_user:
            smtp.login(st.smtp_user, st.smtp_password.get_secret_value() if st.smtp_password else "")
        smtp.send_message(msg)
    return True


class ForgotIn(BaseModel):
    email: str = Field(max_length=254)


@router.post("/password/forgot")
async def forgot(body: ForgotIn) -> dict[str, Any]:
    """Send a reset code. The answer is the same whether or not the email has an account (no account probing)."""
    key = body.email.strip().lower()
    if time.monotonic() - _RESET_SENT.get(key, -1e9) < 60:
        raise HTTPException(status_code=429, detail={"code": "WAIT", "message": "A code was just sent. Wait a minute before asking again."})
    _RESET_SENT[key] = time.monotonic()
    got = deps.get_accounts().start_reset(key)
    if got:
        u, code = got
        try:
            await run_in_threadpool(_send_code, u["email"], u["name"], code)
        except (OSError, smtplib.SMTPException) as exc:
            log.error("couldn't email the reset code: %s", exc)
            raise HTTPException(status_code=503, detail={"code": "EMAIL_FAILED", "message": "Couldn't send the email right now. Try again shortly."}) from None
    return {"sent": True, "message": "If that email has a Kairo account, a 6-digit code is on its way. It expires in 15 minutes."}


class ResetIn(BaseModel):
    email: str = Field(max_length=254)
    code: str = Field(max_length=12)
    new_password: str = Field(max_length=128)


@router.post("/password/reset")
async def reset(body: ResetIn, request: Request, response: Response) -> dict[str, Any]:
    accounts = deps.get_accounts()
    try:
        u = await run_in_threadpool(accounts.finish_reset, body.email, body.code, body.new_password)
    except AccountError as exc:
        raise _fail(exc) from None
    _set_cookie(response, accounts.create_session(u["id"], request.headers.get("user-agent", "")))
    return _me(accounts, u["id"])


# ------------------------------------------------------------------------------------------ your data


@router.get("/export")
async def export(request: Request) -> dict[str, Any]:
    """Everything Kairo holds about you, as JSON (DPDP Act: your right to your data)."""
    _, s = _unlocked(request)
    accounts = deps.get_accounts()
    store = deps.user_store(s["user_id"])
    data: dict[str, Any] = {}
    with store._lock:
        for t in PERSONAL_TABLES:
            data[t] = [dict(r) for r in store._conn.execute(f"SELECT * FROM {t}").fetchall()]
        data["settings"] = {r["key"]: r["value"] for r in store._conn.execute("SELECT key, value FROM settings")
                            if not r["key"].startswith("upstox_token")}
    return {"exported_at": dt.datetime.now(dt.timezone.utc).isoformat(), "account": public_user(accounts.user(s["user_id"])), "data": data}


class DeleteIn(BaseModel):
    password: str = Field(max_length=128)
    confirm: str = Field(max_length=20)


@router.post("/delete")
async def delete_account(body: DeleteIn, request: Request, response: Response) -> dict[str, Any]:
    """Permanently delete the account and all of its data."""
    _, s = _unlocked(request)
    accounts, uid = deps.get_accounts(), s["user_id"]
    if body.confirm.strip().upper() != "DELETE":
        raise HTTPException(status_code=422, detail={"code": "CONFIRM", "message": "Type DELETE to confirm."})
    try:
        await run_in_threadpool(accounts.verify_password, uid, body.password)
    except AccountError as exc:
        raise _fail(exc) from None
    from api.trading import _TRADERS
    store = deps._USER_STORES.get(uid)
    if store is not None:
        trader = _TRADERS.pop(id(store), None)
        if trader and trader.task:
            trader.task.cancel()
    deps.drop_user_store(uid)
    accounts.delete(uid)  # sessions and reset codes go with it
    path = deps.user_db_path(uid)
    for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if p.exists():
            p.unlink()
    response.delete_cookie(deps.AUTH_COOKIE, path="/")
    return {"deleted": True}

