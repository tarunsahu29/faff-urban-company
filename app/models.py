"""Pydantic models for the request/response spine.

The pipeline is:
    text -> ParsedNeed -> ResolvedService + ResolvedLocation -> [Slot] -> BookingReadySlot
"""
from __future__ import annotations

from datetime import date as _date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------- parse step ----------

class TimeMode(str, Enum):
    asap = "asap"          # earliest available slot
    specific = "specific"  # a requested date/time window


class TimePref(BaseModel):
    mode: TimeMode = TimeMode.asap
    # For specific mode: resolved target date + a coarse window of day.
    target_date: Optional[_date] = None
    # 24h window bounds in local time, e.g. evening -> (17, 21). None => any time that day.
    window_start_hour: Optional[int] = None
    window_start_minute: Optional[int] = None  # exact minute for "3:15pm" (0 for a bare "3pm")
    window_end_hour: Optional[int] = None
    raw: str = ""  # original time phrase, for transparency


class ParsedNeed(BaseModel):
    service_need: str          # normalized free-text need, e.g. "deep cleaning 2bhk"
    location_text: str         # building/area to geocode, e.g. "Stellar Heights, Gachibowli"
    flat_number: Optional[str] = None  # flat/house/room number, e.g. "606"
    time_pref: TimePref
    raw_text: str
    parser_backend: str = "heuristic"
    category_key: Optional[str] = None   # UC categoryKey chosen by the LLM (if used)
    category_name: Optional[str] = None
    parser_note: Optional[str] = None    # e.g. why Gemini fell back to heuristic


# ---------- resolve service ----------

class ResolvedService(BaseModel):
    query: str
    service_id: Optional[str] = None   # UC SKU / service id (from catalog capture)
    name: str
    category: Optional[str] = None
    price_display: Optional[str] = None
    duration_display: Optional[str] = None
    match_score: float = 0.0           # fuzzy confidence 0-100
    candidates: list[dict] = Field(default_factory=list)  # runner-up matches (judgment surface)
    resolved: bool = False


# ---------- location / serviceability ----------

class ResolvedLocation(BaseModel):
    query: str
    lat: Optional[float] = None
    lon: Optional[float] = None
    display_name: Optional[str] = None
    pincode: Optional[str] = None
    city: Optional[str] = None
    serviceable: Optional[bool] = None  # None = unknown (serviceability call not wired yet)
    source: Optional[str] = None        # "urban_company" | "nominatim_fallback"
    city_key: Optional[str] = None      # UC city key, e.g. "city_hyderabad_v2" (drives journey)
    place_id: Optional[str] = None      # Google/UC place id (drives journey address)
    raw: dict = Field(default_factory=dict)  # full getLocationForPlaceId payload (for journey)


# ---------- slots ----------

class Slot(BaseModel):
    slot_ref: str              # opaque id/handle we can pass to /book
    date: _date
    start_hour: int
    start_minute: int = 0      # so "3:00 PM" vs "3:30 PM" is distinguishable
    label: str                 # human label, e.g. "Tomorrow, 5:00 PM - 6:00 PM"
    available: bool = True
    raw: dict = Field(default_factory=dict)  # original UC slot object


# ---------- top-level responses ----------

class SlotsRequest(BaseModel):
    text: Optional[str] = None            # single free-text box
    # OR structured 3-box input (either works)
    service_need: Optional[str] = None
    location_text: Optional[str] = None
    time_text: Optional[str] = None
    slot_ref: Optional[str] = None        # book THIS exact slot (from a clicked alternative)


class SlotsResponse(BaseModel):
    parsed: ParsedNeed
    service: ResolvedService
    location: ResolvedLocation
    booking_ready_slot: Optional[Slot] = None
    alternatives: list[Slot] = Field(default_factory=list)
    requested_time_available: Optional[bool] = None  # was the EXACT requested time free?
    package: Optional[dict] = None         # {name, price, duration_mins} added to the cart
    slot_confirmed: Optional[bool] = None  # verified bookable via isSlotAvailableAtCheckout
    draft_order_id: Optional[str] = None   # UC cart/draft the slot is configured in
    checkout_url: Optional[str] = None     # open (logged in) to SEE/complete this exact cart
    address_set: bool = False              # address attached to the draft
    address_label: Optional[str] = None    # the saved address used for the booking
    slot_set: bool = False                 # slot selected into the draft
    payable_amount: Optional[float] = None # total to pay (from updateSlot) — for booking
    status: str                            # "booking_ready" | "no_slot" | "not_serviceable" | "needs_login"
    notes: list[str] = Field(default_factory=list)


class BookRequest(BaseModel):
    # Re-sends the request so /book re-configures the draft fresh before ordering
    # (guards against the checkout page resetting the slot on load).
    text: Optional[str] = None
    service_need: Optional[str] = None
    location_text: Optional[str] = None
    time_text: Optional[str] = None
    slot_ref: Optional[str] = None  # book THIS exact slot (from a clicked alternative)
    confirm: bool = False  # extra guard: must be true to attempt a real booking
