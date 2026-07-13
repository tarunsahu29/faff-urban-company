"""UC booking journey: (categoryKey, location, time) -> a FULLY CONFIGURED cart.

Reproduces UC's stateful checkout and leaves the draft order booking-ready:

    proceedWithAddress          -> save the resolved address (addressId)
    initiateJourney(categoryKey)-> draftOrderId + packageGroup + packages
    updatePackageSelection      -> add the cheapest package
    getNextGroup + loadGroup    -> advance to the slot group
    updateAddressInDraftOrder   -> attach the address
    getCheckoutJourneySlotPage  -> the date/time grid  -> pick best by time_pref
    updateSlot                  -> SELECT that slot into the draft
    isSlotAvailableAtCheckout   -> confirm it's still bookable (else try the next)

Result: the cart holds service + package + address + selected slot — everything but
payment. No professional is dispatched and no money moves (cart-ready = safe).
"""
from __future__ import annotations

import logging
from datetime import date as _date

from .models import Slot, TimePref
from .slots import pick_slot
from .uc_client import deep_get, uc

log = logging.getLogger("uc.booking")

_API = {
    "proceed": "/api/v2/growth/profile/proceedWithAddress",
    "initiate": "/api/v2/growth/customerJourney/initiateJourney",
    "package": "/api/v2/growth/customerJourney/updatePackageSelection",
    "next": "/api/v2/growth/customerJourney/getNextGroup",
    "load": "/api/v2/growth/customerJourney/loadGroup",
    "checkloc": "/api/v2/growth/customerJourney/checkPackageUpdatesAtNewLocation",
    "address": "/api/v2/growth/customerJourney/updateAddressInDraftOrder",
    "slotpage": "/api/v2/marketplace/capacityOrionPL/customerFacing/getCheckoutJourneySlotPage",
    "updateslot": "/api/v2/growth/customerJourney/updateSlot",
    "verify": "/api/v2/marketplace/capacityOrionPL/customerFacing/isSlotAvailableAtCheckout",
    "payopts": "/api/v2/monet/customers/getPaymentOptionsScreen",
    "order": "/api/v2/growth/customerJourney/initiateCheckoutOrder",
}


# ---------- address payloads (built from the resolved location) ----------

def _components(raw: dict) -> dict:
    out = {"city": None, "state": None}
    for c in raw.get("address_components", []) or []:
        types = c.get("types", [])
        if "locality" in types or "administrative_area_level_2" in types:
            out["city"] = out["city"] or c.get("long_name")
        if "administrative_area_level_1" in types:
            out["state"] = c.get("long_name")
    return out


def _location_object(loc, recipient: str) -> dict:
    raw = loc.raw or {}
    comp = _components(raw)
    return {
        "accuracy": 0, "address": "", "city": comp["city"] or (loc.city or ""),
        "city_key": loc.city_key, "name": "Home",
        "recipient_name_obj": {"name": recipient or "Customer", "title": ""},
        "pin_code": raw.get("postalCode") or loc.pincode or "",
        "point": [loc.lon, loc.lat], "show_map": False,
        "google_place_id": loc.place_id, "state": comp["state"] or "",
        "locality": raw.get("formatted_address") or loc.display_name or "",
        "geoProofingLocality": raw.get("geoProofingLocality") or "",
    }


def _proceed_with_address(loc, category_key: str, recipient: str,
                          flat_number: str | None) -> dict:
    """Save/activate the TYPED location as an address (with the flat number the user
    gave — UC treats it as a separate 'house_number' detail) and return the FULL
    activated object UC hands back. That object — not a getUserAddress listing
    object, and never with a blank house number — is what updateAddress accepts."""
    r = uc._request("POST", _API["proceed"],
                    json={"city_key": None, "location": _location_object(loc, recipient),
                          "categoryKey": category_key, "sourceScreen": "summary",
                          "userJourneyCityKey": loc.city_key,
                          "modifiedFields": {"house_number": flat_number or "1"}})
    return (r.get("success", {}).get("data", {}) or {}).get("location") or {}


# ---------- package selection (cheapest, surfaced) ----------

def _pick_package(ij: dict, need_text: str = "") -> tuple[dict | None, dict | None]:
    """Return (package_payload, package_info). Chooses the package whose NAME best
    matches the user's request (e.g. "deep clean 1 bathroom" -> "Intense cleaning
    (1 Bathroom)"). Falls back to UC's first/recommended package when nothing matches
    well — avoids both the trivial add-on (cheapest) and over-sized default."""
    from rapidfuzz import fuzz

    pkgs = deep_get(ij, "packagesData") or {}
    if not isinstance(pkgs, dict) or not pkgs:
        return None, None

    items = list(pkgs.items())  # API order == UC's display order (first = recommended)
    need = (need_text or "").lower()

    # Count hint: "1 bhk"/"1 bathroom"/"one bathroom" -> prefer that many units.
    import re as _re
    m = _re.search(r"\b(\d+)\s*(?:bhk|bathroom|room|ac|unit)", need)
    if not m and any(w in need for w in ("one bathroom", "single bathroom", "1bhk")):
        want_n = 1
    else:
        want_n = int(m.group(1)) if m else None

    best_i, best_score = 0, -1.0
    for i, (_, pd) in enumerate(items):
        base = pd.get("base", {}) or {}
        name = (base.get("name") or "").lower()
        # Names are marketing labels ("Grooming essentials"); the actual service
        # ("Haircut + Beard Grooming") is in the description — match against both.
        blob = f"{name} {(base.get('description') or '').lower()}"
        score = float(fuzz.token_set_ratio(need, blob)) if need else 0.0
        # ₹0 usually means a variant-priced stub or free add-on — prefer a package
        # with a concrete price (e.g. "Grooming essentials" over bare "Haircut").
        if not (pd.get("price", {}) or {}).get("totalCost"):
            score -= 12
        # Count is only a TIEBREAKER among already-relevant packages — never let it
        # promote an off-topic add-on ("Door cleaning (upto 1)") over the real match.
        if want_n is not None and score >= 45:
            nm = _re.search(r"\b(\d+)\b", name)
            if nm:
                score += 8 if int(nm.group(1)) == want_n else -8
        if score > best_score:
            best_score, best_i = score, i

    if best_score >= 55:
        comp_key, pd = items[best_i]
    else:
        # weak match -> UC's first *recommended* package in display order, but skip
        # subscriptions / build-your-own / gifts / free add-ons (₹0), which are not
        # the main service a user means.
        BAD = ("annual", "plan", "subscription", "membership", "amc",
               "make your own", "gift", "combo")
        def ok(it):
            b = it[1].get("base", {}) or {}
            name = (b.get("name") or "").lower()
            cost = (it[1].get("price", {}) or {}).get("totalCost", 0)
            return cost and not any(w in name for w in BAD)
        pool = [it for it in items if ok(it)] or items
        comp_key, pd = pool[0]
    pid = comp_key.split("#")[0]
    base = pd.get("base", {}) or {}
    price = pd.get("price", {}) or {}
    payload = {
        "id": int(pid) if pid.isdigit() else pid, "quantity": 1,
        "variants": [{"id": v["variantId"], "quantity": v.get("quantity", 1)}
                     for v in pd.get("selectedVariants", []) or []],
        "variantOptions": pd.get("selectedVariantOptions", []) or [],
        "name": base.get("name", ""), "type": base.get("sub_type", "tweakable"),
        "packageType": base.get("type", "service_item"), "packageUIType": "DEFAULT",
    }
    info = {"name": base.get("name", ""), "price": price.get("totalCost"),
            "duration_mins": price.get("duration")}
    return payload, info


# ---------- slot grid parsing (store full slot data for updateSlot) ----------

def parse_slots(slot_page: dict) -> list[Slot]:
    grid = (deep_get(slot_page, "slotGridTapActionDS") or {}).get("uc_assist", {})
    out: list[Slot] = []
    seen: set[tuple] = set()
    for date_id, node in grid.items():
        try:
            d = _date.fromisoformat(date_id)
        except ValueError:
            continue

        def walk(o):
            if isinstance(o, dict):
                ota = o.get("onTapAction")
                if isinstance(ota, dict) and isinstance(ota.get("data"), dict) and "slotId" in ota["data"]:
                    yield ota["data"]
                for v in o.values():
                    yield from walk(v)
            elif isinstance(o, list):
                for x in o:
                    yield from walk(x)

        for sd in walk(node):
            sid = sd.get("slotId", "")
            if (date_id, sid) in seen:
                continue
            seen.add((date_id, sid))
            # slotId is "HH:MM" (e.g. "15:00" vs "15:30") — keep BOTH parts so 3:00
            # and 3:30 are distinguishable.
            parts = sid.split(":")
            hour = int(parts[0]) if parts and parts[0].isdigit() else 0
            minute = int(parts[1]) if len(parts) > 1 and parts[1][:2].isdigit() else 0
            out.append(Slot(slot_ref=f"{date_id} {sid}", date=d, start_hour=hour,
                            start_minute=minute,
                            label=sd.get("bookingTimeString") or f"{date_id} {sid}",
                            available=True, raw=sd))
    return out


_AUTO_ASSIGN = {
    "id": "uc_assist", "name": "Urban Company Auto-Assign",
    "profilePhoto": {
        "s3_path": "/images/marketplace/customer-app-marketplace/1632473980683-9ea0c5.png",
        "base_url": "https://www.urbancompany.com/img",
        "low_res": "?bucket=urbanclap-prod&quality=80&format=auto",
        "medium_res": "?bucket=urbanclap-prod&quality=85&format=auto",
        "high_res": "?bucket=urbanclap-prod&quality=90&format=auto"},
}


def _update_slot(draft: str, slot_group: str, slot: Slot, category_key: str) -> dict:
    sd = slot.raw or {}
    return uc._request("POST", _API["updateslot"], json={
        "city_key": None, "draftOrderId": draft, "groupId": slot_group,
        "categoryWiseBookingDetails": [{
            "slotDetails": {
                "bookingStartTime": sd.get("bookingTime"),
                "bookingEndTime": sd.get("bookingEndTime"),
                "bookingTimeStrategy": sd.get("bookingTimeStrategy", "fixed"),
                "hubId": sd.get("hubId"),
                "preferredProvider": _AUTO_ASSIGN,   # auto-assign a professional
                "filterStrategy": sd.get("filterStrategy", "general"),
            },
            "categoryKey": category_key,
        }]})


def _saved_addresses(draft: str) -> list[dict]:
    """The user's saved UC addresses (full objects, usable as selectedAddress)."""
    r = uc._request("POST", "/api/v2/growth/customerJourney/getUserAddress",
                    json={"city_key": None, "draftOrderId": draft, "bundleOptInIds": []})
    out, seen = [], set()

    def walk(o):
        if isinstance(o, dict):
            aid = o.get("addressId") or o.get("_id")
            if aid and "point" in o and ("city_key" in o or "cityKey" in o) and aid not in seen:
                seen.add(aid)
                out.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(r)
    return out


def _initiate(category_key: str, loc, coords: dict, address_id: str | None) -> dict:
    """Start/refresh the booking journey. The address is applied HERE via
    homescreenAddress.addressId (exactly how UC's web does it) — not via a separate
    updateAddress call. initiateJourney is idempotent, so re-calling with the
    address id updates the same draft."""
    return uc._request("POST", _API["initiate"], json={
        "city_key": None, "userId": "", "cityKey": loc.city_key, "countryKey": "IND",
        "dimensions": {"categoryKey": category_key, "cityKey": loc.city_key,
                       "source": "customerApplications", "useCase": "multiCategoryCheckout",
                       "coordinates": {"lng": coords["long"], "lat": coords["lat"]}},
        "triggerSource": {"type": "discovery", "details": {}},
        "dataPoints": {"coordinates": coords, "homescreenAddress": {"addressId": address_id}},
        "screenUrl": f"/cart?city={loc.city_key}&category={category_key}"})


def _addr_label(a: dict) -> str:
    name, flat = a.get("name") or "", a.get("address") or ""
    lbl = _clean(f"{name} - {flat}") if (name or flat) else ""
    return lbl or a.get("locality") or ""


def _resolve_address(draft: str, loc, category_key: str, recipient: str,
                     flat_number: str | None) -> tuple[dict | None, str | None]:
    """Return (FULL activated address object, label) for the address to book at.

    proceedWithAddress activates the typed location (with the user's flat) and hands
    back the full object in the exact shape checkPackageUpdatesAtNewLocation +
    updateAddressInDraftOrder require. A getUserAddress *listing* object is a silent
    no-op for the commit, so we ALWAYS re-activate via proceedWithAddress — reusing
    the user's flat (typed, or the one already saved at this place) so no duplicate
    address is minted. (Capture proof: 02-booking-walk / url-order-walk attach a
    fresh draft's address this way; initiateJourney's homescreenAddress id only
    seeds a brand-new draft, it does not commit onto an existing one.)"""
    flat = flat_number
    if not flat:
        # borrow the flat saved at this exact place so we re-activate that one
        # (not a "1" placeholder) — avoids the junk-address problem.
        try:
            saved = _saved_addresses(draft)
        except Exception:
            saved = []
        same = [a for a in saved
                if (a.get("google_place_id") or a.get("placeId")) == loc.place_id]
        if same:
            flat = same[0].get("address")
    try:
        obj = _proceed_with_address(loc, category_key, recipient, flat)
        if obj.get("_id"):
            return obj, _addr_label(obj)
    except Exception:
        pass
    return None, None


def _check_package_updates(draft: str, slot_group: str, addr_obj: dict) -> dict:
    """checkPackageUpdatesAtNewLocation — re-price/revalidate the cart at the address
    location; the browser fires this right before committing the address."""
    return uc._request("POST", _API["checkloc"], json={
        "city_key": None, "draftOrderId": draft, "inputAddress": addr_obj,
        "groupId": slot_group, "useCase": "multiCategoryCheckoutDesktop"})


def _commit_address(draft: str, slot_group: str, addr_obj: dict) -> dict:
    """updateAddressInDraftOrder — the actual 'commit this address onto the draft'
    call, taking the FULL activated object. This (not initiateJourney's homescreen
    seed) is what attaches an address to an already-minted draft."""
    return uc._request("POST", _API["address"], json={
        "city_key": None, "draftOrderId": draft, "groupId": slot_group,
        "selectedAddress": addr_obj, "useCase": "multiCategoryCheckoutDesktop"})


def _clean(text: str) -> str:
    """Strip UC's inline markup like '{ `Home` <textType:small-sb/> } - 606' -> 'Home - 606'."""
    import re as _re
    return _re.sub(r"\s+", " ", _re.sub(r"[{}`]|<[^>]+>", "", text or "")).strip(" -")


def _checkout_state(lg: dict) -> dict:
    """Read the REAL selected address + slot from the checkout summary
    (checkoutBookingJourney) — the exact cards the checkout page renders. This is
    the source of truth, unlike the addressText prefetch field."""
    cbj = deep_get(lg, "checkoutBookingJourney") or {}
    out = {"address_selected": False, "address_text": None,
           "slot_selected": False, "slot_text": None}
    for it in cbj.get("items", []) if isinstance(cbj, dict) else []:
        iid = it.get("id")
        if iid not in ("addressCard", "slotCard"):
            continue
        texts: list[str] = []

        def gt(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k == "text" and isinstance(v, str):
                        texts.append(v)
                    gt(v)
            elif isinstance(o, list):
                for x in o:
                    gt(x)
        gt(it)
        joined = " ".join(texts).lower()
        primary = _clean(texts[0]) if texts else None
        if iid == "addressCard":
            out["address_selected"] = bool(texts) and "select address" not in joined
            out["address_text"] = primary
        else:
            out["slot_selected"] = bool(texts) and "select" not in joined
            out["slot_text"] = primary
    return out


def _slot_available(draft: str, slot_group: str, category_key: str, city_key: str) -> bool | None:
    """True/False from isSlotAvailableAtCheckout, or None if the response doesn't
    carry the flag — we must NOT claim "confirmed available" on an unknown."""
    r = uc._request("POST", _API["verify"], json={
        "city_key": None, "city": city_key, "customerCategoryKey": category_key,
        "draftOrderId": draft, "groupId": slot_group, "screenSource": "checkout",
        "isSlotValidationRequired": True})
    val = deep_get(r, "isSlotAvailable")
    if val is None:
        val = deep_get(r, "isAvailable")
    return bool(val) if val is not None else None  # None = unknown (don't claim confirmed)


# ---------- orchestration ----------

def build_booking(category_key: str, loc, time_pref: TimePref, need_text: str = "",
                  flat_number: str | None = None, slot_ref: str | None = None) -> dict:
    """Configure a booking-ready cart and return everything needed for the result.

    When slot_ref is given (the user clicked a specific alternative), that exact slot
    is selected instead of re-picking the nearest to the time preference."""
    result = {"slots": [], "selected": None, "alternatives": [], "package": None,
              "available": None, "draft_order_id": None, "note": None,
              "checkout_url": None, "address_set": False, "slot_set": False,
              "payable_amount": None, "address_label": None, "exact_match": None}
    if not loc or loc.lat is None or not loc.city_key:
        return result

    from . import auth
    sess = auth.current_session()
    recipient = (sess.name if sess and sess.name and sess.name.lower() != "verified customer"
                 else "Customer")
    coords = {"lat": loc.lat, "long": loc.lon}

    # 1) start the journey to mint a draft. SINGLE initiate — the capture never
    #    re-initiates to attach an address (that only seeds a brand-new draft).
    ij = _initiate(category_key, loc, coords, None)
    draft = deep_get(deep_get(ij, "journeyConfig") or {}, "draftOrderId")
    if not draft:
        result["note"] = "Could not start a booking journey for this service."
        return result
    result["draft_order_id"] = draft
    package_group = deep_get(deep_get(ij, "journeyConfig") or {}, "groupId") or deep_get(ij, "groupId")

    # 2) resolve the FULL activated address object (proceedWithAddress) to commit
    #    onto the draft below — NOT via initiateJourney's homescreen seed.
    addr_obj, addr_label = _resolve_address(draft, loc, category_key, recipient, flat_number)
    if addr_obj:
        result["address_label"] = addr_label
    else:
        result["note"] = "No address could be selected — add one on Urban Company first."

    # 3) add the package that best matches the request
    package, info = _pick_package(ij, need_text)
    result["package"] = info
    if package:
        uc._request("POST", _API["package"], json={
            "city_key": None, "draftOrderId": draft, "packages": [package],
            "groupId": package_group, "coordinates": coords, "shouldFetchNextPrice": False,
            "productContext": {"carouselMappings": []}, "source": "customerApplications",
            "useCase": "multiCategoryCheckoutDesktop", "categoryKey": category_key})

    # 4) advance to the slot/checkout group — getNextGroup returns its id in data.data.id
    ng = uc._request("POST", _API["next"], json={
        "city_key": None, "draftOrderId": draft, "currentGroupId": package_group,
        "source": "customerApplications", "useCase": "multiCategoryCheckoutDesktop",
        "cityKey": loc.city_key, "navigationStack": []})
    ng_group = (ng.get("success", {}).get("data", {}).get("data", {}) or {})
    slot_group = ng_group.get("id") or package_group
    result["slot_group"] = slot_group  # cached so a slot change can skip the full re-config
    if ng_group.get("screenUrl"):
        result["checkout_url"] = uc.WEB_ORIGIN + ng_group["screenUrl"]

    # 5) COMMIT the address onto the draft at the checkout group — the real attach
    #    (checkPackageUpdatesAtNewLocation -> updateAddressInDraftOrder, full object),
    #    exactly as 02-booking-walk / url-order-walk do for a fresh draft.
    if addr_obj:
        try:
            _check_package_updates(draft, slot_group, addr_obj)
            _commit_address(draft, slot_group, addr_obj)
        except Exception as e:  # noqa: BLE001
            log.warning("BOOKING draft=%s | address commit failed: %s", draft, e)

    # loadGroup renders the checkout cards AFTER the address commit — its addressCard
    # is the source of truth for whether the address actually stuck. (Runs BEFORE the
    # slot so nothing re-renders on the committed slot right before the order.)
    lg = uc._request("POST", _API["load"], json={
        "city_key": None, "draftOrderId": draft, "currentGroupId": slot_group,
        "source": "customerApplications", "useCase": "multiCategoryCheckoutDesktop",
        "cityKey": loc.city_key, "navigationStack": []})
    st = _checkout_state(lg)
    result["address_set"] = st["address_selected"]
    if st["address_text"]:
        result["address_label"] = st["address_text"]

    # slot page -> parse -> pick (honour an explicitly clicked slot_ref)
    sp = uc._request("POST", _API["slotpage"], json={
        "city_key": None, "city": loc.city_key, "customerCategoryKey": category_key,
        "groupId": slot_group, "draftOrderId": draft, "action": "createNewRequest"})
    all_slots = parse_slots(sp)
    result["slots"] = all_slots
    if not all_slots:
        return result

    chosen, alts, exact = pick_slot(all_slots, time_pref)
    if slot_ref:  # user clicked a specific alternative — book exactly that one
        picked = next((s for s in all_slots if s.slot_ref == slot_ref), None)
        if picked:
            alts = [s for s in all_slots if s.slot_ref != picked.slot_ref][:5]
            chosen, exact = picked, True
        else:
            result["note"] = "That time is no longer available — picked the nearest instead."
    result["selected"] = chosen
    result["alternatives"] = alts
    result["exact_match"] = exact

    # 6) SELECT the slot. The AUTHORITATIVE order-ready signal is updateSlot's
    #    checkoutState == 'ready_for_payment' (payableAmount alone is echoed even on a
    #    not-ready draft, e.g. address uncommitted — which is how a fresh draft used to
    #    slip through and then journey-error at the order).
    checkout_state = None
    if chosen:
        try:
            us = _update_slot(draft, slot_group, chosen, category_key)
            result["payable_amount"] = deep_get(us, "payableAmount")
            checkout_state = deep_get(us, "checkoutState")
            result["available"] = _slot_available(draft, slot_group, category_key, loc.city_key)
        except Exception as e:  # noqa: BLE001
            result["available"] = None
            log.warning("BOOKING draft=%s | updateSlot failed: %s", draft, e)
    result["slot_set"] = (checkout_state == "ready_for_payment") and result["available"] is not False

    # NO loadGroup after updateSlot — canonical order window is
    # updateSlot -> isSlotAvailableAtCheckout -> getPaymentOptionsScreen -> order.
    log.info("BOOKING draft=%s group=%s | address=%r set=%s (card_says=%s) | slot=%r "
             "exact=%s amount=%s checkoutState=%s available=%s -> slot_set=%s",
             draft, slot_group, result["address_label"], result["address_set"],
             st["address_selected"], chosen.label if chosen else None, exact,
             result["payable_amount"], checkout_state, result["available"], result["slot_set"])
    return result


def payment_eligibility(payopts_resp: dict) -> dict:
    """Extract COD / pay-later eligibility from a getPaymentOptionsScreen response —
    exactly what the UI needs to explain WHY a Cash-on-Delivery order was refused."""
    md = ((payopts_resp.get("success", {}) or {}).get("data", {}) or {}).get("metadata", {}) or {}
    acr = md.get("autoCashRequest", {}) or {}
    pld = (md.get("updatedRequestData", {}) or {}).get("payLaterDetails", {}) or {}
    return {
        "cod_applicable": acr.get("isCODApplicable"),
        "high_demand_disabled": acr.get("isPayAfterServiceDisabledDueToHighDemand"),
        "pay_later_disabled": pld.get("isDisabled"),
        "reasons": pld.get("disableReasons") or [],
        "cash_surcharge": md.get("cashSurchargeAmount"),
    }


def _arm_payment(draft_order_id: str, amount: float, city_key: str,
                 category_key: str) -> dict:
    """Register the Monet payment flow for this draft. UC's checkout calls
    getPaymentOptionsScreen (flow=CUSTOMER_SELLING_JOURNEY, flowId=draftOrderId)
    right before initiateCheckoutOrder — without it the order has no payment
    context and comes back with no checkoutOrderId. (05-placed-order-new.har.)"""
    resp = uc._request("POST", _API["payopts"], json={
        "city_key": None,
        "customer": {"id": "", "eligibilityDetails": {}},
        "location": {"city": city_key},
        "payment": {"flow": "CUSTOMER_SELLING_JOURNEY", "flowId": draft_order_id,
                    "amount": amount},
        "emiDetails": {"isApplicable": False},
        "areUCCreditsApplied": False,
        "categoryKey": category_key, "categoryKeys": [category_key],
        "thirdPartyDetails": {},
        "journeyDetails": {"id": draft_order_id, "type": "REGULAR"},
        "couponDetails": {"appliedCouponDetails": {}},
        "payLaterDetails": {"isDisabled": False, "isHidden": False, "disableReasons": [],
                            "allowedPaymentMediumsForPayLater": ["CASH", "ONLINE"]},
        "amountToRedeemUcCreditsOn": amount, "skipCache": False,
        "sourceFlow": "preRequest", "ucCreditsToBeApplied": 0})
    # Log the account's CURRENT COD eligibility — this is what decides whether the
    # order can go through as Cash-on-Delivery (the capture is from when COD worked;
    # only this runtime response shows why it might be refused now).
    e = payment_eligibility(resp)
    log.info("PAYOPTS draft=%s | isCODApplicable=%s highDemandDisabled=%s "
             "payLaterDisabled=%s reasons=%s cashSurcharge=%s",
             draft_order_id, e["cod_applicable"], e["high_demand_disabled"],
             e["pay_later_disabled"], e["reasons"], e["cash_surcharge"])
    return resp


def select_slot_only(draft: str, slot_group: str, category_key: str, city_key: str,
                     slot: Slot) -> dict:
    """FAST slot change: the draft is already configured (address + package committed
    on a prior build_booking), so switching the time only needs updateSlot + a
    freshness check — 2 calls instead of the full ~10-call journey. Used by the
    /select_slot preview so clicking an alternative is quick. (Capture proof: in
    05-placed-order-new.har the user re-picked the slot with a bare updateSlot on the
    same group, no re-commit of address/package.)"""
    us = _update_slot(draft, slot_group, slot, category_key)
    amount = deep_get(us, "payableAmount")
    checkout_state = deep_get(us, "checkoutState")
    available = _slot_available(draft, slot_group, category_key, city_key)
    slot_set = (checkout_state == "ready_for_payment") and available is not False
    log.info("SELECT draft=%s group=%s | slot=%r amount=%s checkoutState=%s "
             "available=%s -> slot_set=%s", draft, slot_group, slot.label, amount,
             checkout_state, available, slot_set)
    return {"payable_amount": amount, "available": available, "slot_set": slot_set}


def place_order_cod(draft_order_id: str, amount: float, city_key: str | None = None,
                    category_key: str | None = None) -> tuple[dict, dict]:
    """Place the order with Pay-later CASH (Cash on Delivery) — the real booking.
    No card/UPI gateway. Arms the payment flow first (required), then places the
    order. Returns (initiateCheckoutOrder response, payment_eligibility) — the latter
    lets the UI explain a COD refusal (e.g. pending dues / high demand)."""
    eligibility: dict = {}
    if city_key and category_key:
        try:
            payopts = _arm_payment(draft_order_id, amount, city_key, category_key)
            eligibility = payment_eligibility(payopts)
        except Exception as e:  # noqa: BLE001 — log, but still attempt the order
            log.warning("ORDER draft=%s | arm_payment failed: %s", draft_order_id, e)
    resp = uc._request("POST", _API["order"], json={
        "city_key": None,
        "journeyDetails": {"id": draft_order_id, "type": "REGULAR"},
        "initiatePaymentPayload": {
            "isSystemInitiated": False,
            "isPayLaterSelected": True,
            "sourceBreakup": [{"mode": "CASH_ON_DELIVERY", "amount": amount}],
        }})
    import json as _json
    data = (resp.get("success", {}) or {}).get("data", {}) or {}
    # Dump the full (small) data blob so a journey error's TYPE/reason is visible.
    try:
        blob = _json.dumps(data)[:600] if isinstance(data, dict) else str(data)[:600]
    except Exception:
        blob = "<unserializable>"
    log.info("ORDER draft=%s | isError=%s checkoutOrderId=%s isJourneyError=%s "
             "journeyErrorType=%s | data=%s",
             draft_order_id, resp.get("isError"),
             data.get("checkoutOrderId") if isinstance(data, dict) else None,
             data.get("isJourneyError") if isinstance(data, dict) else None,
             data.get("journeyErrorType") if isinstance(data, dict) else None, blob)
    return resp, eligibility


def order_outcome(resp: dict) -> dict:
    """Interpret an initiateCheckoutOrder response as GROUND TRUTH.

    A real booking returns a `checkoutOrderId` with `isJourneyError: false` (see the
    successful order in 05-placed-order-new.har). `isError == false` ALONE is not
    enough — the call can succeed at the transport layer yet journey-error (e.g. no
    address/slot committed) and place nothing. We only report a booking when an
    order id actually came back."""
    data = (resp.get("success", {}) or {}).get("data", {}) or {}
    order_id = data.get("checkoutOrderId")
    journey_error = bool(data.get("isJourneyError")) or bool(resp.get("isError"))
    jtype = data.get("journeyErrorType")
    # Human-readable help for the journey error types we understand.
    _HELP = {
        "payment_mode_not_allowed":
            "Cash-on-Delivery is not allowed for this booking right now. The cart is "
            "fully order-ready (address + slot committed) — UC is refusing the COD "
            "payment mode itself, which it gates by live demand and account history "
            "(e.g. recent cancellations). Try again later or with a different service; "
            "the only other option is online prepayment.",
    }
    # Only ERROR-specific fields — never a generic `message` (UC nests success-y
    # widget messages like "Successfully Done!!" that would masquerade as a reason).
    message = (resp.get("err_message") or deep_get(resp, "errorMessage")
               or deep_get(data, "errorText") or deep_get(data, "journeyErrorText")
               or _HELP.get(jtype)
               or (f"journey error: {jtype}" if jtype else None))
    return {
        "placed": bool(order_id) and not journey_error,
        "order_id": order_id,
        "line_items": (data.get("lineItems", {}) or {}).get("created", []),
        "journey_error": journey_error,
        "journey_error_type": jtype,
        "message": message,
    }
