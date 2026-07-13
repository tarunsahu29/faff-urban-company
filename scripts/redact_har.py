"""Strip live credentials out of HAR capture files, IN PLACE.

Run this on every HAR before it's analyzed or committed:

    python scripts/redact_har.py                 # scrub all captures/*.har
    python scripts/redact_har.py captures/new.har # scrub one file

Redacts: Authorization/Bearer tokens, cookies, session/device ids, API tokens,
Google Maps keys/signatures, and — in request/response bodies — the login token,
Cloudflare integrityToken, entered OTP, passwords, and phone numbers.

Kept intact: UI-layout keys that merely CONTAIN "token"/"otp" in their name
(colorToken, OTPCard, resendOTPButton, …) — matched by exact key so the layout
structure the app code reads stays valid. Names/addresses are left as-is (the
address-resolution code needs them; they're your own data on your own machine).
"""
from __future__ import annotations

import glob
import json
import re
import sys

# Header names (lowercase) whose VALUE is a credential.
SENSITIVE_HEADERS = {
    "authorization", "cookie", "set-cookie", "x-session-id", "x-device-id",
    "x-api-token", "www-authenticate", "x-goog-api-key", "x-client-data",
    "x-goog-maps-api-signature", "x-goog-maps-session-id", "x-goog-maps-api-salt",
    "x-goog-gmp-client-signals",
}

# Exact JSON keys (with quotes) whose value is a secret. Long-value guard avoids
# nuking short enums; OTP/password/secret are redacted at any length.
_KEY_LONG = re.compile(
    r'("(?:token|integrityToken|clientAuthToken|refreshToken|accessToken|idToken|'
    r'authToken|apiToken|sessionToken)"\s*:\s*")[^"]{12,}(")')
_KEY_ANY = re.compile(r'("(?:OTPValue|otp|password|secret|integritytoken)"\s*:\s*")[^"]*(")',
                      re.IGNORECASE)
_BEARER = re.compile(r'(Bearer\s+)[A-Za-z0-9._\-]{16,}')
_JWT = re.compile(r'eyJ[A-Za-z0-9._\-]{16,}')
_PHONE = re.compile(r'\b(?:\+?91[\-\s]?)?[6-9]\d{9}\b')
_QS = re.compile(r'([?&](?:key|signature|sig|token|auth|api_key|apikey)=)[^&"\s]+',
                 re.IGNORECASE)


def scrub_text(s: str | None) -> str | None:
    if not s:
        return s
    s = _BEARER.sub(r"\1REDACTED", s)
    s = _JWT.sub("REDACTED_JWT", s)
    s = _KEY_LONG.sub(r"\1REDACTED\2", s)
    s = _KEY_ANY.sub(r"\1REDACTED\2", s)
    s = _QS.sub(r"\1REDACTED", s)
    s = _PHONE.sub("REDACTED_PHONE", s)
    return s


def scrub_headers(headers: list) -> None:
    for h in headers or []:
        if h.get("name", "").lower() in SENSITIVE_HEADERS:
            h["value"] = "REDACTED"
        else:
            h["value"] = scrub_text(h.get("value"))


def scrub_cookies(cookies: list) -> None:
    for c in cookies or []:
        c["value"] = "REDACTED"


def redact_file(path: str) -> None:
    har = json.load(open(path))
    for e in har.get("log", {}).get("entries", []):
        req, resp = e.get("request", {}), e.get("response", {})
        req["url"] = _QS.sub(r"\1REDACTED", req.get("url", ""))
        scrub_headers(req.get("headers", []))
        scrub_headers(resp.get("headers", []))
        scrub_cookies(req.get("cookies", []))
        scrub_cookies(resp.get("cookies", []))
        for qs in req.get("queryString", []) or []:
            if qs.get("name", "").lower() in ("key", "signature", "sig", "token",
                                              "auth", "api_key", "apikey"):
                qs["value"] = "REDACTED"
        if req.get("postData", {}).get("text"):
            req["postData"]["text"] = scrub_text(req["postData"]["text"])
        if resp.get("content", {}).get("text"):
            resp["content"]["text"] = scrub_text(resp["content"]["text"])
    blob = json.dumps(har, ensure_ascii=False, indent=1)
    json.loads(blob)  # validate before overwriting
    with open(path, "w") as fh:
        fh.write(blob)


def verify(path: str) -> list[str]:
    """Return any residual secret-looking strings (should be empty)."""
    txt = open(path, encoding="utf-8", errors="ignore").read()
    hits = []
    for name, pat in (("Bearer", r"Bearer [A-Za-z0-9._\-]{16,}"),
                      ("JWT", r"eyJ[A-Za-z0-9._\-]{16,}"),
                      ("phone", r"\b[6-9]\d{9}\b")):
        if re.search(pat, txt):
            hits.append(name)
    return hits


def main() -> None:
    targets = sys.argv[1:] or sorted(glob.glob("captures/*.har"))
    if not targets:
        print("no HAR files found (pass a path or run from the repo root)")
        return
    for path in targets:
        redact_file(path)
        leftover = verify(path)
        flag = f"  ⚠ residual: {leftover}" if leftover else "  ✓ clean"
        print(f"redacted {path}{flag}")


if __name__ == "__main__":
    main()
