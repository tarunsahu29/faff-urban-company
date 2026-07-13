"""Bonus — booking assembly with a HARD no-pay guard.

Two phases, deliberately separated:
  1. assemble()  — builds the booking-ready payload (service + slot + address).
                   SAFE: no dispatch, no charge. This is the natural extension of
                   the MVP's "booking-ready slot".
  2. confirm()   — the real dispatch + payment. Blocked by DEFAULT. Requires BOTH:
                     - settings.allow_real_booking == True  (env ALLOW_REAL_BOOKING)
                     - an explicit confirm=true on the request
                   Per the chosen scope ("build bonus, don't book yet"), this stays
                   off until you flip the env flag for a single, cancellable booking.
"""
from __future__ import annotations

from .auth import current_session
from .config import settings
from .uc_client import NeedsCapture, uc


def assemble(service_id: str, slot_ref: str, address: dict | None = None) -> dict:
    """Phase 1: booking-ready payload. No money, no dispatch."""
    session = current_session()
    return {
        "ready": True,
        "authenticated": session is not None,
        "payload": {
            "service_id": service_id,
            "slot_ref": slot_ref,
            "address": address or {},
        },
        "next_step": "POST /book with confirm=true (blocked unless ALLOW_REAL_BOOKING=true)",
    }


def confirm(service_id: str, slot_ref: str, address: dict | None = None,
            confirm_flag: bool = False) -> dict:
    """Phase 2: REAL booking. Guarded three ways."""
    if not confirm_flag:
        return {"ok": False, "blocked": True,
                "error": "confirm=true is required for a real booking."}
    if not settings.allow_real_booking:
        return {"ok": False, "blocked": True,
                "error": "ALLOW_REAL_BOOKING is off. Real dispatch + payment disabled. "
                         "Set it in .env only when you're ready for one cancellable booking."}
    if current_session() is None:
        return {"ok": False, "blocked": True,
                "error": "Not authenticated. Complete OTP login (or set UC_AUTH_TOKEN/UC_COOKIE)."}
    try:
        result = uc.create_booking(service_id, slot_ref, address or {})
        return {"ok": True, "booking": result}
    except NeedsCapture as e:
        return {"ok": False, "error": str(e), "surface": e.surface}
