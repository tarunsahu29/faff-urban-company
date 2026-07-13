"""Auth — dynamic UC session via the human-assisted browser login.

Primary flow (`browser_login`): open UC's real login in a headful browser, the
human clears the Cloudflare Turnstile + OTP, we intercept the fresh Bearer token
from validateLogin and arm the API client. The token lives in memory only.

The API-only OTP helpers (`start_login`/`complete_login`) remain for the case
where you already hold a valid Turnstile `integrity_token`; they're not the
default path since Cloudflare blocks automated captcha solving.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from . import browser_login
from .config import settings
from .uc_client import uc


@dataclass
class Session:
    phone: str
    token: str
    cookie: str = ""
    name: str = ""


# In-memory only. Cleared on restart. Never persisted.
_SESSION: Session | None = None

# Progress of an in-flight browser login, polled by the UI.
_LOGIN: dict = {"status": "idle", "message": ""}  # status: idle|running|done|error
_LOGIN_LOCK = threading.Lock()


def current_session() -> Session | None:
    global _SESSION
    if _SESSION:
        return _SESSION
    if settings.uc_auth_token or settings.uc_cookie:
        return Session(phone="(env override)", token=settings.uc_auth_token,
                       cookie=settings.uc_cookie)
    return None


def _greeting(s: "Session | None") -> str | None:
    if not s:
        return None
    name = (s.name or "").strip()
    if name and name.lower() != "verified customer":
        return name
    return s.phone or None


def status() -> dict:
    s = current_session()
    return {
        "authenticated": uc.is_authenticated(),
        "phone": s.phone if s else None,
        "name": s.name if s else None,
        "greeting": _greeting(s),
        "device_id": uc.device_id,
        "login": dict(_LOGIN),
    }


# ---------------------------------------------------------------------------
# Human-assisted browser login (default path)
# ---------------------------------------------------------------------------

def _run_browser_login(phone: str) -> None:
    global _SESSION
    result = browser_login.interactive_login(phone)
    with _LOGIN_LOCK:
        if result.get("ok"):
            user = result.get("user") or {}
            uc.set_session(token=result["token"])
            _SESSION = Session(phone=user.get("phone") or phone,
                               token=result["token"], name=user.get("name") or "")
            _LOGIN.update(status="done",
                          message=f"Signed in as {_greeting(_SESSION) or 'you'}")
        else:
            _LOGIN.update(status="error", message=result.get("error", "login failed"))


def browser_login_start(phone: str = "") -> dict:
    """Launch the interactive browser login in the background; UI polls status()."""
    with _LOGIN_LOCK:
        if _LOGIN["status"] == "running":
            return {"ok": False, "error": "A login is already in progress."}
        if uc.is_authenticated():
            return {"ok": True, "already": True}
        _LOGIN.update(status="running",
                      message="Complete the login in the opened browser window "
                              "(verify you're human + enter the OTP).")
    threading.Thread(target=_run_browser_login, args=(phone,), daemon=True).start()
    return {"ok": True, "started": True}


# ---------------------------------------------------------------------------
# API-only OTP helpers (used only when an integrity_token is already available)
# ---------------------------------------------------------------------------

_PENDING_PHONE: str = ""


def start_login(phone: str, integrity_token: str = "") -> dict:
    global _PENDING_PHONE
    if not integrity_token:
        return {"ok": False, "needs_captcha": True,
                "error": "initiateLogin needs a Cloudflare Turnstile token. Use the "
                         "browser login (POST /auth/browser-login) instead."}
    try:
        uc.bootstrap_guest()
        resp = uc.request_otp(phone, integrity_token)
        data = resp.get("success", {}).get("data", {})
        _PENDING_PHONE = phone
        return {"ok": True, "sent": True, "login_uuid": data.get("uuid"),
                "secret_length": data.get("secretLength")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def complete_login(otp: str, login_uuid: str = "") -> dict:
    global _SESSION
    try:
        resp = uc.verify_otp(otp, login_uuid)
        data = resp.get("success", {}).get("data", {})
        token = data.get("token", "")
        user = data.get("userData", {}) or {}
        if not token:
            return {"ok": False, "error": "No token in validateLogin response."}
        _SESSION = Session(phone=user.get("phone") or _PENDING_PHONE, token=token)
        return {"ok": True, "authenticated": uc.is_authenticated(),
                "user": {"phone": user.get("phone"), "name": user.get("name")}}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def logout() -> None:
    """Clear the in-memory session AND the persistent Chrome profile, so the next
    Sign in is a genuine fresh login (not an instant reconnect)."""
    global _SESSION
    _SESSION = None
    uc.clear_session()
    browser_login.close_current()  # close any open login Chrome window
    with _LOGIN_LOCK:
        _LOGIN.update(status="idle", message="")
    try:
        import shutil
        if browser_login._CHROME_PROFILE.exists():
            shutil.rmtree(browser_login._CHROME_PROFILE, ignore_errors=True)
    except Exception:
        pass
