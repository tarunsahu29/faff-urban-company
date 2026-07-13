"""Free-text service need -> UC categoryKey (the key that drives the booking journey).

UC's catalog is a fixed set of ~23 category keys (from getDiscoveryScreen). They're
self-descriptive, so we resolve a free-text need by fuzzy-matching it against each
category's keywords. The list can be refreshed live per city (fetch_live_categories),
but the curated map below is the authority for matching + display names.

initiateJourney then returns the packages for the chosen category, so we never need
a per-service capture — the package is picked from the live response.
"""
from __future__ import annotations

from rapidfuzz import fuzz

from .models import ResolvedService
from .uc_client import NeedsCapture, deep_get, uc

# categoryKey -> (display name, keyword string for matching).
# Keys verified live from getDiscoveryScreen.
CATEGORIES: dict[str, tuple[str, str]] = {
    "salon_at_home":                 ("Salon for Women", "salon women beauty waxing facial manicure pedicure haircut threading"),
    "women_hair_services":           ("Hair Services for Women", "hair women haircut hair spa colour keratin women salon"),
    "mg_luxe":                       ("Men's Salon & Grooming", "salon men grooming haircut beard shave mens trim"),
    "salon_luxe":                    ("Salon Luxe (premium)", "salon luxe premium facial manicure pedicure waxing"),
    "massage_for_men":               ("Massage for Men", "massage men spa relax pain relief body massage"),
    "spa_at_home":                   ("Spa for Women", "spa women massage relaxation therapy"),
    "spa_ayurveda":                  ("Ayurveda Spa", "ayurveda spa massage abhyanga wellness"),
    "professional_bathroom_cleaning":("Bathroom Cleaning", "bathroom toilet washroom cleaning deep clean"),
    "professional_kitchen_cleaning": ("Kitchen Cleaning", "kitchen cleaning chimney degrease deep clean"),
    "insta_maids":                   ("InstaHelp / Maid", "maid help cleaning house sweeping mopping instahelp on demand helper"),
    "pest_control":                  ("Pest Control", "pest control cockroach termite mosquito ants bugs"),
    "ac_service_repair":             ("AC Service & Repair", "ac air conditioner cooling service repair gas not cooling"),
    "geyser_reapir":                 ("Geyser Repair", "geyser water heater repair not heating"),
    "gas_stove_repair":              ("Gas Stove Repair", "gas stove hob repair burner"),
    "tv_repair":                     ("TV Repair", "tv television repair screen not working"),
    "laptop_repair":                 ("Laptop Repair", "laptop computer repair screen keyboard"),
    "ro_repair":                     ("Water Purifier Repair", "ro water purifier repair service filter"),
    "ro_purchase":                   ("Water Purifier (New)", "ro water purifier buy purchase new install"),
    "plumbers_density":              ("Plumber", "plumber plumbing tap leak pipe drainage flush"),
    "electrician_density":           ("Electrician", "electrician wiring switch fan light fitting mcb"),
    "carpenters_density":            ("Carpenter", "carpenter furniture door hinge drill bed cupboard"),
    "painting_shp_sku_survey":       ("Painting", "paint painting wall waterproofing"),
    "wall_makeover":                 ("Wall Makeover / Panels", "wall panels makeover texture decor"),
    "epc_stores_smarthome":          ("Smart Home Devices", "smart home lock camera automation device native"),
}


def fetch_live_categories(city_key: str | None, lat: float | None,
                          lon: float | None) -> list[str]:
    """Refresh the serviceable categoryKeys for a city from getDiscoveryScreen.
    Best-effort — falls back to the curated CATEGORIES keys."""
    if not (city_key and lat is not None):
        return list(CATEGORIES)
    try:
        r = uc._request(
            "POST", "/api/v2/growth/customerHomescreen/getDiscoveryScreen",
            json={"city_key": None, "cityKey": city_key, "customerId": "",
                  "countryKey": "IND",
                  "locationDetails": {"lat": lat, "long": lon}})
        found: set[str] = set()

        def walk(o):
            if isinstance(o, dict):
                ck = o.get("categoryKey") or o.get("category_key")
                if isinstance(ck, str) and ck not in ("NA", ""):
                    found.add(ck)
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for x in o:
                    walk(x)
        walk(r)
        return sorted(found) or list(CATEGORIES)
    except Exception:
        return list(CATEGORIES)


def resolve_service(need: str, city_key: str | None = None,
                    lat: float | None = None, lon: float | None = None,
                    preset_category_key: str | None = None) -> ResolvedService:
    # If the LLM already picked a valid category, trust it (high confidence).
    if preset_category_key and preset_category_key in CATEGORIES:
        name, _ = CATEGORIES[preset_category_key]
        return ResolvedService(query=need, service_id=preset_category_key, name=name,
                               category=preset_category_key, match_score=100.0,
                               candidates=[], resolved=True)

    need_l = (need or "").lower().strip()
    live = set(fetch_live_categories(city_key, lat, lon))

    # Gender cue -> nudge gender-specific categories (salon/spa are gendered).
    men = any(w in f" {need_l} " for w in (" men ", " male ", " gents", " boy", " mens", " husband"))
    women = any(w in f" {need_l} " for w in (" women", " female", " ladies", " lady", " girl", " wife"))
    MEN_CATS = {"mg_luxe", "massage_for_men"}
    WOMEN_CATS = {"salon_at_home", "women_hair_services", "spa_at_home"}

    scored: list[tuple[str, float]] = []
    for ck, (name, keywords) in CATEGORIES.items():
        blob = f"{name} {keywords} {ck.replace('_', ' ')}".lower()
        score = fuzz.token_set_ratio(need_l, blob)
        if live and ck not in live:  # not serviceable here
            score -= 8
        if men and ck in WOMEN_CATS:
            score -= 25
        if women and ck in MEN_CATS:
            score -= 25
        if men and ck in MEN_CATS:
            score += 12
        if women and ck in WOMEN_CATS:
            score += 12
        scored.append((ck, float(score)))
    scored.sort(key=lambda t: t[1], reverse=True)

    best_ck, best_score = scored[0]
    name, _ = CATEGORIES[best_ck]
    candidates = [{"category_key": ck, "name": CATEGORIES[ck][0], "score": round(sc, 1)}
                  for ck, sc in scored[1:4]]
    return ResolvedService(
        query=need,
        service_id=best_ck,          # categoryKey drives the journey
        name=name,
        category=best_ck,
        match_score=round(best_score, 1),
        candidates=candidates,
        resolved=best_score >= 55,   # below this, matching is too weak to trust
    )
