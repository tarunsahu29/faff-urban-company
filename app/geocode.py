"""Location text -> coordinates + address context.

Mirrors UC's real behaviour: **UC's own location autocomplete is the primary
geocoder** (that's what the app itself uses — you type an address, UC returns
place suggestions with coordinates). OpenStreetMap Nominatim is only a *fallback*
for when the UC endpoint isn't wired yet or returns nothing, so the spine still
runs before the location HAR is captured.
"""
from __future__ import annotations

import httpx

from .config import settings
from .models import ResolvedLocation
from .uc_client import NeedsCapture, uc

_NOMINATIM = "https://nominatim.openstreetmap.org/search"


def _from_uc(location_text: str) -> ResolvedLocation | None:
    """Primary path: UC's own autocomplete + place resolution. Gives coordinates,
    cityKey (serviceable city), pincode and placeId — everything the booking
    journey needs. Returns None if unavailable so we can fall back to Nominatim."""
    try:
        suggestions = uc.location_autocomplete(location_text)
    except Exception:
        return None
    if not suggestions:
        return None

    top = suggestions[0]
    try:
        place = uc.get_location_for_place(top)
    except Exception:
        return None

    lat = place.get("latitude") or (place.get("geometry", {}).get("location", {}).get("lat"))
    lon = place.get("longitude") or (place.get("geometry", {}).get("location", {}).get("lng"))
    city_key = place.get("cityKey") or place.get("city_key")
    return ResolvedLocation(
        query=location_text,
        lat=float(lat) if lat is not None else None,
        lon=float(lon) if lon is not None else None,
        display_name=place.get("formatted_address") or top.get("description"),
        pincode=place.get("postalCode"),
        city=place.get("geoProofingLocality") or (city_key or "").replace("city_", "").replace("_v2", "").title() or None,
        # UC returning a cityKey means it operates there => serviceable.
        serviceable=bool(city_key),
        source="urban_company",
        city_key=city_key,
        place_id=place.get("placeId") or top.get("placeId"),
        raw=place,
    )


def _from_nominatim(location_text: str, country_bias: str = "in") -> ResolvedLocation:
    """Fallback geocoder (no key). Used only until the UC location HAR is wired."""
    params = {
        "q": location_text,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": 1,
        "countrycodes": country_bias,
    }
    headers = {"User-Agent": settings.nominatim_user_agent}
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(_NOMINATIM, params=params, headers=headers)
            r.raise_for_status()
            results = r.json()
    except Exception:
        return ResolvedLocation(query=location_text)
    if not results:
        return ResolvedLocation(query=location_text)

    top = results[0]
    addr = top.get("address", {})
    city = (addr.get("city") or addr.get("town") or addr.get("state_district")
            or addr.get("county") or addr.get("state"))
    return ResolvedLocation(
        query=location_text,
        lat=float(top["lat"]),
        lon=float(top["lon"]),
        display_name=top.get("display_name"),
        pincode=addr.get("postcode"),
        city=city,
        serviceable=None,
        source="nominatim_fallback",
    )


def geocode(location_text: str) -> ResolvedLocation:
    q = (location_text or "").strip()
    if not q:
        return ResolvedLocation(query=q)
    # UC first (real behaviour), Nominatim only as fallback.
    return _from_uc(q) or _from_nominatim(q)
