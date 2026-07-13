"""FastAPI service. Pipeline:

    text -> parse -> resolve service -> geocode + serviceability -> slots -> pick

The whole spine runs even before the UC endpoints are captured: the read
surfaces degrade to status "needs_capture" so you can demo parse/resolve/geocode
end-to-end, then light up slots the moment the capture is wired.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

# Surface booking-flow logs (address/slot/amount per query) in the console.
_h = logging.StreamHandler(sys.stderr)
_h.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
_booklog = logging.getLogger("uc.booking")
if not _booklog.handlers:
    _booklog.addHandler(_h)
_booklog.setLevel(logging.INFO)
_booklog.propagate = False

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel

from . import auth, booking, catalog, geocode, journey, parse, slots
from .config import settings
from .models import (BookRequest, ResolvedLocation, Slot, SlotsRequest,
                     SlotsResponse)
from .uc_client import NeedsCapture, uc

app = FastAPI(title="faff — Home Services Booking (Urban Company)", version="0.1.0")

_STATIC = Path(__file__).parent.parent / "static"
if _STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


# In-memory cache of the last fully-configured booking draft (single-user local
# tool). Lets /select_slot switch the time by reusing the committed draft instead of
# re-running the whole ~10-call journey. Cleared on logout.
_LAST_BOOKING: dict = {}


@app.on_event("startup")
def _fresh_login_each_start() -> None:
    """Every app start begins logged out — clear any persisted browser session so
    the next Sign in is a genuine fresh login (per user preference)."""
    auth.logout()


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    idx = _STATIC / "index.html"
    if idx.exists():
        return idx.read_text()
    return "<h1>faff Home Services</h1><p>POST /slots with {text} or {service_need, location_text, time_text}</p>"


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "parser_backend": settings.parser_backend,
        "authenticated": auth.current_session() is not None,
        "endpoints_wired": {
            "auth": "wired", "location": "wired",
            "catalog": "wired", "slots_journey": "wired",
        },
    }


def _configure(req: "SlotsRequest"):
    """Shared pipeline: text -> parse -> location -> service -> configured draft.
    Returns (parsed, location, service, booking_result_or_None)."""
    if req.text:
        parsed = parse.parse_text(req.text)
    else:
        parsed = parse.parse_structured(req.service_need or "", req.location_text or "",
                                        req.time_text or "")
    location = geocode.geocode(parsed.location_text)
    service = catalog.resolve_service(parsed.service_need, city_key=location.city_key,
                                      lat=location.lat, lon=location.lon,
                                      preset_category_key=parsed.category_key)
    b = None
    if (service.service_id and location.lat is not None and auth.current_session()
            and location.serviceable is not False):
        b = journey.build_booking(service.service_id, location, parsed.time_pref,
                                  need_text=parsed.service_need or (req.text or ""),
                                  flat_number=parsed.flat_number, slot_ref=req.slot_ref)
    return parsed, location, service, b


@app.post("/slots", response_model=SlotsResponse)
def get_booking_ready_slot(req: SlotsRequest) -> SlotsResponse:
    notes: list[str] = []

    # 0) AUTH — login is the required first step; the booking journey is session-gated.
    if not auth.current_session():
        notes.append("Not signed in — use 'Sign in with Urban Company' first; the "
                     "booking journey needs your session.")

    # 1) PARSE ---------------------------------------------------------------
    if req.text:
        parsed = parse.parse_text(req.text)
    else:
        parsed = parse.parse_structured(
            req.service_need or "", req.location_text or "", req.time_text or "")

    if parsed.parser_note:
        notes.append(parsed.parser_note)

    # 2) LOCATION (UC's own geocode -> coords + cityKey; drives everything) ---
    location = geocode.geocode(parsed.location_text)
    if location.lat is None:
        notes.append("Could not resolve the location.")

    # 3) RESOLVE SERVICE -> categoryKey (serviceability-aware for the city) ---
    service = catalog.resolve_service(parsed.service_need, city_key=location.city_key,
                                      lat=location.lat, lon=location.lon,
                                      preset_category_key=parsed.category_key)
    if not service.resolved:
        notes.append(f"Couldn't confidently match '{parsed.service_need}' to a UC "
                     f"service (best guess: {service.name}).")

    # 4) BOOKING JOURNEY -> configured cart (service + package + address + slot) ---
    booking_ready: Slot | None = None
    alternatives: list[Slot] = []
    requested_time_available: bool | None = None
    package = None
    slot_confirmed = None
    draft_id = None
    checkout_url = None
    address_set = False
    address_label = None
    slot_set = False
    payable_amount = None
    status = "no_slot"

    if location.serviceable is False:
        status = "not_serviceable"
        notes.append("Location is outside UC's serviceable cities.")
    elif not auth.current_session():
        status = "needs_login"
        notes.append("Sign in first — the booking journey needs your session.")
    elif service.service_id and location.lat is not None:
        try:
            b = journey.build_booking(service.service_id, location, parsed.time_pref,
                                      need_text=parsed.service_need or (req.text or ""),
                                      flat_number=parsed.flat_number, slot_ref=req.slot_ref)
            booking_ready = b["selected"]
            alternatives = b["alternatives"]
            requested_time_available = b.get("exact_match")
            package = b["package"]
            slot_confirmed = b["available"]
            draft_id = b["draft_order_id"]
            checkout_url = b.get("checkout_url")
            address_set = b.get("address_set", False)
            address_label = b.get("address_label")
            slot_set = b.get("slot_set", False)
            payable_amount = b.get("payable_amount")
            # Cache the configured draft so a slot CHANGE (via /select_slot) can reuse
            # it and skip the full re-config.
            if draft_id and b.get("slot_group") and service.service_id:
                _LAST_BOOKING.clear()
                _LAST_BOOKING.update({
                    "draft": draft_id, "slot_group": b["slot_group"],
                    "category_key": service.service_id, "city_key": location.city_key,
                    "slots_by_ref": {s.slot_ref: s for s in (b.get("slots") or [])},
                })
            if package:  # surface price/duration on the service line too
                service.price_display = f"₹{package['price']}" if package.get("price") else None
                service.duration_display = (f"{package['duration_mins']} mins"
                                            if package.get("duration_mins") else None)
            if b.get("note"):
                notes.append(b["note"])
            if booking_ready:
                status = "booking_ready"
                if slot_confirmed is False:
                    notes.append("Picked slot just became unavailable — showing alternatives.")
                elif requested_time_available is False:
                    notes.append(f"Your requested time wasn't free — nearest is "
                                 f"{booking_ready.label}. Pick another below if you like.")
            elif b["slots"]:
                notes.append("Slots exist but none matched; nearest shown.")
            else:
                notes.append("No slots returned for this service/location.")
        except Exception as e:  # noqa: BLE001
            status = "error"
            notes.append(f"Booking journey failed: {type(e).__name__}: {e}")
    else:
        notes.append("Need a resolved service + location before fetching slots.")

    return SlotsResponse(
        parsed=parsed,
        service=service,
        location=location,
        booking_ready_slot=booking_ready,
        alternatives=alternatives,
        requested_time_available=requested_time_available,
        package=package,
        slot_confirmed=slot_confirmed,
        draft_order_id=draft_id,
        checkout_url=checkout_url,
        address_set=address_set,
        address_label=address_label,
        slot_set=slot_set,
        payable_amount=payable_amount,
        status=status,
        notes=notes,
    )


class SelectSlotRequest(BaseModel):
    slot_ref: str  # a slot_ref from the last /slots response's alternatives
    draft_order_id: Optional[str] = None  # the view's draft — rejects a moved cache


@app.post("/select_slot")
def select_slot(req: SelectSlotRequest) -> dict:
    """Fast slot switch for the UI: reuse the last fully-configured draft and only
    re-run updateSlot for the chosen time (no order placed). Falls back to a full
    search client-side if there's no cached draft. Returns ok + slot_set + amount."""
    if not auth.current_session():
        return {"ok": False, "error": "not_signed_in"}
    ctx = _LAST_BOOKING
    if not ctx.get("draft") or not ctx.get("slot_group"):
        return {"ok": False, "error": "no_context"}   # UI should fall back to /slots
    if req.draft_order_id and ctx.get("draft") != req.draft_order_id:
        # cache was overwritten by another search/tab — force a full reconfigure
        return {"ok": False, "error": "stale_context"}
    slot = (ctx.get("slots_by_ref") or {}).get(req.slot_ref)
    if slot is None:
        return {"ok": False, "error": "slot_gone"}    # stale ref -> UI falls back
    try:
        r = journey.select_slot_only(ctx["draft"], ctx["slot_group"],
                                     ctx["category_key"], ctx["city_key"], slot)
        return {"ok": True, "slot_ref": req.slot_ref, "slot_set": r["slot_set"],
                "payable_amount": r["payable_amount"], "available": r["available"]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# =====================================================================
# BONUS — auth + booking (built, but stops before pay by design)
# =====================================================================

class OtpStart(BaseModel):
    phone: str
    integrity_token: str = ""  # Cloudflare Turnstile token from the browser helper


class OtpVerify(BaseModel):
    otp: str
    login_uuid: str = ""       # from the initiateLogin response


class BrowserLogin(BaseModel):
    phone: str = ""


@app.get("/auth/status")
def auth_status() -> dict:
    return auth.status()


@app.post("/auth/browser-login")
def browser_login(req: BrowserLogin) -> dict:
    """Default login: opens UC's real login in a browser for the human handshake,
    then intercepts the session token. Poll /auth/status for completion."""
    return auth.browser_login_start(req.phone)


@app.post("/auth/logout")
def logout() -> dict:
    _LAST_BOOKING.clear()
    auth.logout()
    return {"ok": True}


@app.post("/auth/otp/start")
def otp_start(req: OtpStart) -> dict:
    return auth.start_login(req.phone, req.integrity_token)


@app.post("/auth/otp/verify")
def otp_verify(req: OtpVerify) -> dict:
    return auth.complete_login(req.otp, req.login_uuid)


@app.post("/book")
def book(req: BookRequest) -> dict:
    """Place the REAL order (Pay-later CASH / Cash on Delivery). Re-configures the
    draft fresh (address + slot) right before ordering so a checkout-page reset
    can't break it, then places the order. A professional gets scheduled — cancel
    from Urban Company afterwards.

    Triple-guarded: needs confirm=true AND ALLOW_REAL_BOOKING=true AND a session."""
    if not req.confirm:
        return {"ok": False, "blocked": True,
                "error": "confirm=true required for a real booking."}
    if not settings.allow_real_booking:
        return {"ok": False, "blocked": True,
                "error": "ALLOW_REAL_BOOKING is off. Set it in .env only when you're "
                         "ready for one cancellable Cash-on-Delivery booking."}
    if not auth.current_session():
        return {"ok": False, "blocked": True, "error": "Not signed in."}
    try:
        _, location, service, b = _configure(SlotsRequest(
            text=req.text, service_need=req.service_need,
            location_text=req.location_text, time_text=req.time_text,
            slot_ref=req.slot_ref))
        if not b or not b.get("draft_order_id"):
            return {"ok": False, "error": "Could not configure a booking for that request."}
        if not b.get("address_set"):
            return {"ok": False, "error": "Your address couldn't be attached to the cart",
                    "why": "Urban Company didn't accept the address for this booking.",
                    "next_step": "Check the address (building + flat number) and search again.",
                    "reason_code": "address_not_committed"}
        if not b.get("slot_set") or not b.get("payable_amount"):
            return {"ok": False, "error": "The slot couldn't be locked into the cart",
                    "why": "The chosen time didn't reach a ready-for-payment state (it may have just filled).",
                    "next_step": "Pick another nearby time and try again.",
                    "reason_code": "slot_not_committed"}
        resp, eligibility = journey.place_order_cod(
            b["draft_order_id"], b["payable_amount"],
            city_key=location.city_key, category_key=service.service_id)
        outcome = journey.order_outcome(resp)
        if not outcome["placed"]:
            # The call may return isError:false yet place nothing (journey error) —
            # never report a booking without a real checkoutOrderId. Explain WHY in
            # plain language (esp. COD refusals: pending dues / high demand).
            headline, why, step = _explain_booking_failure(
                outcome.get("journey_error_type"), eligibility, outcome.get("message"))
            return {"ok": False, "error": headline, "why": why, "next_step": step,
                    "reason_code": outcome.get("journey_error_type"),
                    "cart_ready": True, "order_id": None}
        return {"ok": True,
                "service": service.name,
                "amount": b["payable_amount"],
                "slot": b["selected"].label if b.get("selected") else None,
                "address": b.get("address_label"),
                "order_id": outcome["order_id"],
                "note": (f"Order placed (Cash on Delivery) — order {outcome['order_id']}. "
                         "Cancel from Urban Company → Bookings if this was a test.")}
    except Exception as e:  # noqa: BLE001
        name = type(e).__name__
        transient = "500" in str(e) or "timeout" in str(e).lower()
        return {"ok": False,
                "error": ("Urban Company had a temporary server error — please try again."
                          if transient else f"Something went wrong: {name}"),
                "why": (str(e)[:200] if not transient else
                        "UC's checkout returned a 5xx (their circuit-breaker); it's usually transient."),
                "next_step": "Tap Book again in a moment." if transient else None,
                "reason_code": name}


def _explain_booking_failure(reason_code, eligibility, raw_message):
    """Turn a journey-error code + payment eligibility into (headline, why, next_step)
    the UI can show plainly."""
    reasons = [str(r).lower() for r in (eligibility or {}).get("reasons", [])]
    if reason_code == "payment_mode_not_allowed":
        headline = "Cash-on-Delivery is currently disabled on your Urban Company account"
        if any("arrear" in r or "due" in r or "recovery" in r for r in reasons):
            return (headline,
                    "Reason: pending dues on your account (e.g. an unpaid cancellation fee) — "
                    "UC blocks COD until they're cleared.",
                    "Clear the pending amount in the Urban Company app, then tap Book again. "
                    "Your cart (service, address, slot) is already order-ready.")
        if (eligibility or {}).get("high_demand_disabled") or any("demand" in r for r in reasons):
            return (headline,
                    "Reason: high demand — UC has temporarily turned COD off in your area.",
                    "Try again shortly, or pick a different time/service. The cart is ready.")
        detail = f" (reason: {', '.join(reasons)})" if reasons else ""
        return (headline,
                f"Urban Company isn't allowing COD for this booking right now{detail}.",
                "Try again later, or use online prepayment in the UC app. The cart is ready.")
    # Fallbacks for other journey errors.
    return ("Urban Company couldn't place the order",
            raw_message or "The cart wasn't order-ready (address/slot not committed).",
            "Re-run the search and try again.")
