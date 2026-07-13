"""Human-assisted login that actually passes Cloudflare Turnstile.

Key lesson: letting Playwright/patchright *launch* the browser bakes in automation
flags (--enable-automation, CDP pipe) that Turnstile detects — so even a genuine
human click fails with "Verification failed".

Fix: we DON'T launch via Playwright. We start the user's REAL Google Chrome as an
ordinary app (no automation flags, navigator.webdriver=false) pointed at UC, and
merely ATTACH over the DevTools port to listen for the validateLogin response and
lift the session token. Turnstile sees a real browser; the human clears the check
+ OTP once; the agent captures the fresh Bearer token and drives the API.

A dedicated, reused Chrome profile keeps Cloudflare's clearance cookie so repeat
logins usually skip the checkbox.
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

_CHROME_PROFILE = Path(__file__).parent.parent / ".uc_chrome_profile"
ORIGIN_URL = "https://www.urbancompany.com/"
_PORT = 9222
_CURRENT_PROC = None  # the live login Chrome, so we can close it on logout/re-login


def close_current() -> None:
    """Kill the login Chrome window if one is open (called on logout / re-login)."""
    global _CURRENT_PROC
    proc = _CURRENT_PROC
    _CURRENT_PROC = None
    if proc is None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
    except Exception:
        pass

_CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome"),
    shutil.which("google-chrome-stable"),
    shutil.which("chromium"),
    shutil.which("chromium-browser"),
]


def _find_chrome() -> str | None:
    for c in _CHROME_CANDIDATES:
        if c and Path(c).exists():
            return c
    return None


def _free_port(start: int) -> int:
    for port in range(start, start + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:  # nothing listening
                return port
    return start


def _wait_cdp(port: int, timeout_s: float = 25.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version",
                                        timeout=1.5):
                return True
        except Exception:
            time.sleep(0.4)
    return False


def _pick_uc_page(ctx):
    for pg in ctx.pages:
        if "urban" in (pg.url or ""):
            return pg
    return ctx.pages[0] if ctx.pages else ctx.new_page()


def _open_login_modal(page) -> bool:
    """Open UC's login modal so the phone screen is immediately visible.

    UC's account icon is the RIGHTMOST button in the top bar (classes are hashed,
    so we locate it geometrically, not by selector). Click it, then the 'Login'
    item. Returns False (no-op) if already logged in — there's no 'Login' item then.

    This happens BEFORE the Turnstile widget loads and only opens the modal; the
    human still ticks 'verify you're human' + enters the OTP.
    """
    try:
        center = page.evaluate(
            """() => {
              let best = null;
              for (const el of document.querySelectorAll('button,[role=button]')) {
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0 || r.top > 90) continue;
                if (!best || r.x > best.x) best = {x: r.x + r.width/2, y: r.y + r.height/2};
              }
              return best;
            }"""
        )
        if not center:
            return False
        page.mouse.click(center["x"], center["y"])   # account icon
        page.wait_for_timeout(900)
        page.click("text=Login", timeout=2500)        # the 'Login' menu item
        page.wait_for_timeout(600)
        return True
    except Exception:
        return False


def interactive_login(phone: str = "", timeout_ms: int = 300000) -> dict:
    """Launch real Chrome, let the human log in, intercept the token via CDP.
    Returns {ok, token, user, error}. Blocking (run from a worker thread)."""
    chrome = _find_chrome()
    if not chrome:
        return {"ok": False, "error": "Google Chrome not found. Install Chrome, or "
                "we can switch to a paid captcha solver."}

    global _CURRENT_PROC
    close_current()  # close any leftover login window before opening a new one
    _CHROME_PROFILE.mkdir(exist_ok=True)
    port = _free_port(_PORT)
    proc = subprocess.Popen(
        [chrome, f"--remote-debugging-port={port}",
         f"--user-data-dir={_CHROME_PROFILE}",
         "--no-first-run", "--no-default-browser-check", "--new-window",
         "--start-maximized", "--window-position=0,0", "--window-size=1920,1200",
         ORIGIN_URL],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _CURRENT_PROC = proc
    try:
        if not _wait_cdp(port):
            return {"ok": False, "error": "Chrome DevTools port didn't open."}

        from patchright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()

            captured: dict = {}

            def from_response(resp):
                # validateLogin body carries token + userData (name/phone).
                try:
                    if "validateLogin" in resp.url and resp.request.method == "POST":
                        d = (resp.json().get("success", {}) or {}).get("data", {}) or {}
                        if d.get("token"):
                            captured.setdefault("token", d["token"])
                            captured.setdefault("user", d.get("userData", {}) or {})
                            captured.setdefault("is_new", d.get("isUserNew"))
                except Exception:
                    pass

            def from_request(req):
                # Robust fallback: the first authenticated API call after login
                # carries `authorization: Bearer <token>` — grab it directly.
                if "token" in captured:
                    return
                try:
                    if "urbanclap.com/api" in req.url:
                        av = (req.all_headers() or {}).get("authorization", "")
                        if av.lower().startswith("bearer "):
                            captured.setdefault("token", av.split(" ", 1)[1])
                except Exception:
                    pass

            def wire(pg):
                pg.on("response", from_response)
                pg.on("request", from_request)

            for pg in ctx.pages:
                wire(pg)
            ctx.on("page", wire)

            page = _pick_uc_page(ctx)

            # Belt-and-suspenders maximize (some macOS builds ignore --start-maximized).
            try:
                sess = ctx.new_cdp_session(page)
                wid = sess.send("Browser.getWindowForTarget")["windowId"]
                sess.send("Browser.setWindowBounds",
                          {"windowId": wid, "bounds": {"windowState": "maximized"}})
            except Exception:
                pass

            # If the persistent profile is already logged in, the token arrives on
            # its own within a couple of seconds — no login needed.
            for _ in range(3):
                page.wait_for_timeout(1000)
                if "token" in captured:
                    break
            # Otherwise open the login modal for the user, so the phone screen is
            # right there (no hunting the top-right account icon). Safe: this runs
            # BEFORE Turnstile loads and only opens the modal — we never touch the
            # human-check itself, which the user still ticks (plus the OTP).
            if "token" not in captured:
                _open_login_modal(page)

            # Poll (pumping Playwright events) until a token appears or we time out.
            deadline = time.time() + timeout_ms / 1000.0
            while time.time() < deadline and "token" not in captured:
                try:
                    page.wait_for_timeout(1000)  # pumps the event loop => listeners fire
                except Exception:
                    time.sleep(1)

            # Actually CLOSE the Chrome window (not just detach CDP). proc is only
            # Chrome's launcher, so terminating it leaves the window open — send the
            # CDP Browser.close command to shut the real browser.
            try:
                page = _pick_uc_page(ctx)
                ctx.new_cdp_session(page).send("Browser.close")
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass

            token = captured.get("token", "")
            if not token:
                return {"ok": False, "error": "login not completed (timed out)"}
            user = captured.get("user", {}) or {}
            return {"ok": True, "token": token,
                    "user": {"phone": user.get("phone"), "name": user.get("name"),
                             "is_new": captured.get("is_new")}}
    finally:
        close_current()  # robustly terminate (and kill) the login Chrome
