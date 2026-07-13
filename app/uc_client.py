"""Urban Company web client — isolated so the harness could generalize to a 4th app.

Built on curl_cffi with Chrome impersonation (defeats TLS/JA3 fingerprinting the
way Blinkit needed). Secrets come from env. Rate-limit aware (backs off on 429).

>>> HOW TO WIRE AFTER RECON <<<
Each read surface below has a TODO seam. Once you capture the real request from
DevTools (Copy as cURL + response JSON), fill in:
  - the URL + method
  - required query params / body
  - which response fields map to our models
Until then, every call raises NeedsCapture, and the API degrades gracefully to a
"needs_capture" status instead of crashing — so the rest of the spine is testable.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any, Optional

from curl_cffi import requests as cffi_requests

from .config import settings

# A stable per-install device id. Some OTP flows tie request-otp + verify-otp to the
# same device fingerprint, so we persist one id across restarts (gitignored file).
_DEVICE_FILE = Path(__file__).parent.parent / ".uc_device_id"


def _device_id() -> str:
    # UC's web device id looks like "v-1783889701" (v- + a counter). We persist a
    # stable one per install so OTP request+verify share a device fingerprint.
    try:
        if _DEVICE_FILE.exists():
            return _DEVICE_FILE.read_text().strip()
        did = "v-" + uuid.uuid4().int.__str__()[:10]
        _DEVICE_FILE.write_text(did)
        return did
    except OSError:
        return "v-" + uuid.uuid4().int.__str__()[:10]


class NeedsCapture(Exception):
    """Raised by an endpoint that hasn't been wired from a DevTools capture yet."""

    def __init__(self, surface: str):
        self.surface = surface
        super().__init__(f"UC '{surface}' endpoint not wired yet — capture it in Phase 0 recon.")


def deep_get(obj: Any, key: str) -> Any:
    """First occurrence of `key` anywhere in a nested dict/list (UC's server-driven
    UI buries useful fields deep in widget trees)."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = deep_get(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for x in obj:
            found = deep_get(x, key)
            if found is not None:
                return found
    return None


class UrbanCompanyClient:
    # From 01-login.har: the web app is urbancompany.com but the API is urbanclap.com
    # (UC's original brand). Origin/referer stay urbancompany.com.
    API_BASE = "https://www.urbanclap.com"
    WEB_ORIGIN = "https://www.urbancompany.com"
    # App version pinned from the capture; bump when UC ships a new web build.
    VERSION_CODE = "4.273.58"
    BUNDLE_VERSION = "798"  # react-bundle-version — gates the "live" journey config

    def __init__(self) -> None:
        # One persistent session => curl_cffi keeps a cookie jar (e.g. Cloudflare's
        # __cf_bm) across the whole login->browse->book flow.
        self._session = cffi_requests.Session(impersonate="chrome")
        self.device_id = _device_id()          # "v-..." persisted per install
        self.session_uuid = str(uuid.uuid4())  # x-session-id, one per process run
        self._token: str = ""                  # Bearer token (set by validateLogin)
        self._login_uuid: str = ""             # from initiateLogin, used by validateLogin
        self._headers: dict[str, str] = self._base_headers()
        # Optional dev override: paste a Bearer token from a browser session.
        if settings.uc_auth_token:
            self.set_session(token=settings.uc_auth_token)
        if settings.uc_cookie:
            self._headers["cookie"] = settings.uc_cookie

    def _base_headers(self) -> dict[str, str]:
        """The fixed header set every UC API call carries (from the HAR).

        react-bundle-version identifies the live web build — without it UC serves a
        'UcNotLive' journey config with no draftOrderId, so it's required."""
        return {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
            "cache-control": "no-cache",
            "content-type": "application/json",
            "origin": self.WEB_ORIGIN,
            "referer": self.WEB_ORIGIN + "/",
            "react-bundle-version": self.BUNDLE_VERSION,
            "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
            "x-brand-key": "urbanCompany",
            "x-device-id": self.device_id,
            "x-device-os": "desktop_web",
            "x-preferred-language": "english",
            "x-session-id": self.session_uuid,
            "x-version-code": self.VERSION_CODE,
            "x-version-name": f"web_v{self.VERSION_CODE}",
        }

    # ---- session management ----
    def set_session(self, token: str = "", cookie: str = "") -> None:
        """Attach a freshly minted session. UC uses 'authorization: Bearer <token>'."""
        if token:
            self._token = token
            self._headers["authorization"] = f"Bearer {token}"
        if cookie:
            self._headers["cookie"] = cookie

    def clear_session(self) -> None:
        self._token = ""
        self._headers.pop("authorization", None)
        self._headers.pop("cookie", None)

    def is_authenticated(self) -> bool:
        return bool(self._token or self._headers.get("cookie"))

    # ---- low-level request with 429 backoff (good-citizen guardrail) ----
    def _request(self, method: str, path_or_url: str, *, retries: int = 3,
                 **kw: Any) -> Any:
        url = path_or_url if path_or_url.startswith("http") else self.API_BASE + path_or_url
        headers = {**self._headers, **kw.pop("headers", {})}
        backoff = 1.0
        for attempt in range(retries):
            resp = self._session.request(method, url, headers=headers, timeout=20, **kw)
            # Retry rate-limits and transient server errors (UC 5xx flicker under load).
            if resp.status_code == 429 or (resp.status_code in (500, 502, 503)
                                           and attempt < retries - 1):
                retry_after = float(resp.headers.get("retry-after", backoff))
                time.sleep(retry_after)
                backoff *= 2
                continue
            resp.raise_for_status()
            # Be polite between successful calls too.
            time.sleep(0.4)
            try:
                return resp.json()
            except Exception:
                return resp.text
        raise RuntimeError(f"UC rate-limited after {retries} retries: {url}")

    # =================================================================
    # READ SURFACES (MVP — likely anonymous or session-only)
    # =================================================================

    def search_catalog(self, query: str, city: Optional[str] = None) -> list[dict]:
        """Surface 1: service catalog / search -> list of service objects.

        TODO(recon): replace with the real endpoint, e.g.
            return self._request("GET", "/api/.../search",
                                  params={"query": query, "city": city or settings.uc_city})
        Expected to return items with at least: id/SKU, name, category, price, duration.
        """
        raise NeedsCapture("catalog/search")

    def location_autocomplete(self, query: str) -> list[dict]:
        """UC's own address autocomplete (searchLocation). Returns place suggestions
        with placeId + text, which feed getLocationForPlaceId."""
        r = self._request(
            "POST", "/api/v2/growth/locations/searchLocation",
            json={"city_key": None, "userId": "", "sourceScreen": "homescreen",
                  "searchString": query})
        items = (deep_get(r, "searchResultsCard") or {}).get("items", [])
        out: list[dict] = []
        for it in items:
            if not str(it.get("id", "")).startswith("searchResult"):
                continue
            pid = deep_get(it, "placeId")
            if not pid:
                continue
            out.append({
                "placeId": pid,
                "mainText": deep_get(it, "mainText") or "",
                "description": deep_get(it, "description") or "",
                "structuredFormatting": deep_get(it, "structuredFormatting") or {},
            })
        return out

    def get_location_for_place(self, place: dict) -> dict:
        """Resolve a place suggestion -> coordinates + cityKey + pincode + address.
        This is UC's authoritative geocode + city resolution."""
        return (self._request(
            "POST", "/api/v2/growth/locations/getLocationForPlaceId",
            json={"city_key": None, "placeId": place["placeId"],
                  "mainText": place.get("mainText", ""),
                  "description": place.get("description", ""),
                  "saveLocationSearch": False,
                  "structuredFormatting": place.get("structuredFormatting", {})})
            .get("success", {}).get("data", {}) or {})

    def get_slots(self, service_id: str, lat: float, lon: float,
                  date_iso: str) -> list[dict]:
        """Surface 3 (HEART OF MVP): available date/time slots.

        TODO(recon): e.g.
            return self._request("GET", "/api/.../slots",
                                  params={"serviceId": service_id, "lat": lat,
                                          "lon": lon, "date": date_iso})
        Expected: list of slot objects with a time + an opaque slot ref/id + availability.
        """
        raise NeedsCapture("slots")

    # =================================================================
    # AUTH SURFACE (login — the REQUIRED first step; dynamic session, no
    # hardcoded cookie). Captured from 01-login.har.
    # =================================================================

    def bootstrap_guest(self) -> dict:
        """Fetches login consent policies. Not required for auth, but it's the
        call the page makes on the login screen and warms the Cloudflare cookie."""
        return self._request(
            "POST", "/api/v2/growth/consentManagement/getPoliciesForLogin",
            json={"city_key": None, "country": "IND"})

    def request_otp(self, phone: str, integrity_token: str,
                    country_id: str = "IND") -> dict:
        """initiateLogin -> sends the OTP SMS. Requires a Cloudflare Turnstile
        `integrity_token` (captcha) minted by a real browser. Returns a `uuid`
        that validateLogin needs. REAL SMS — only call on explicit user action."""
        data = self._request(
            "POST", "/api/v2/growth/web/initiateLogin",
            json={
                "city_key": None,
                "countryId": country_id,
                "phoneNumber": phone,
                "integrityToken": integrity_token,
                "integrityType": "captcha",
                "userType": "customer",
                "loginType": "otp",
            })
        self._login_uuid = (data.get("success", {}).get("data", {}) or {}).get("uuid", "")
        return data

    def verify_otp(self, otp: str, login_uuid: str = "") -> dict:
        """validateLogin -> exchanges the OTP for a Bearer token, then arms the
        session. No captcha needed here."""
        data = self._request(
            "POST", "/api/v2/growth/web/validateLogin",
            json={
                "uuid": login_uuid or self._login_uuid,
                "secret": otp,
                "device": {"name": "web"},
            })
        token = (data.get("success", {}).get("data", {}) or {}).get("token", "")
        if token:
            self.set_session(token=token)
        return data

    # =================================================================
    # WRITE SURFACE (BONUS — real money/dispatch)
    # =================================================================

    def create_booking(self, service_id: str, slot_ref: str,
                       address: dict) -> dict:
        """Surface 4/6: assemble + confirm booking. REAL DISPATCH + PAYMENT.

        Guarded upstream so this only runs on explicit confirm. TODO(recon).
        """
        raise NeedsCapture("book")


# Singleton used by the pipeline.
uc = UrbanCompanyClient()
