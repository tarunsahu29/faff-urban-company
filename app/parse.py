"""Free-text -> ParsedNeed.

Default backend is a deterministic heuristic parser (no API key, offline).
Optionally swap in Gemini/OpenAI via PARSER_BACKEND for messier inputs.

Examples handled:
  "deep clean my 2BHK in Koramangala tomorrow evening"
  "need a bathroom cleaning ASAP near HSR"
  "book a salon appointment in Indiranagar this saturday at 3pm"
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import dateparser
import httpx

from .config import settings
from .models import ParsedNeed, TimeMode, TimePref

# Coarse time-of-day windows (local 24h).
_DAYPARTS = {
    "morning": (8, 12),
    "noon": (12, 14),
    "afternoon": (14, 17),
    "evening": (17, 21),
    "night": (20, 23),
}

# ASAP / instant intent cues.
_ASAP_CUES = ("asap", "instant", "right now", "right away", "immediately",
              "earliest", "as soon as possible", "now")

# Location prepositions that usually precede a place.
_LOC_PREP = r"\b(?:in|near|at|around|to)\b"


def _detect_time(text: str) -> TimePref:
    low = text.lower()

    # ASAP mode wins if an instant cue is present and no explicit date word.
    has_asap = any(cue in low for cue in _ASAP_CUES)

    # Day-of-week / relative-day / clock references imply a specific target.
    daypart = next((d for d in _DAYPARTS if d in low), None)
    clock = re.search(r"\b(\d{1,2})\s*(?::\s*(\d{2}))?\s*(am|pm)\b", low)
    rel_day = re.search(r"\b(today|tonight|tomorrow|day after tomorrow|"
                        r"mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|"
                        r"fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b", low)

    if has_asap and not (daypart or clock or rel_day):
        return TimePref(mode=TimeMode.asap, raw=_extract_time_phrase(text) or "asap")

    # Try to resolve a concrete date from the phrase.
    target = None
    if rel_day or daypart or clock:
        parsed_dt = dateparser.parse(
            text,
            settings={"PREFER_DATES_FROM": "future", "RELATIVE_BASE": datetime.now()},
        )
        if parsed_dt:
            target = parsed_dt.date()

    ws = we = wsm = None
    if clock:
        hour = int(clock.group(1)) % 12
        if clock.group(3) == "pm":
            hour += 12
        wsm = int(clock.group(2)) if clock.group(2) else 0  # exact minute ("3:15pm" -> 15)
        ws, we = hour, min(hour + 1, 23)
    elif daypart:
        ws, we = _DAYPARTS[daypart]

    if target or ws is not None:
        return TimePref(
            mode=TimeMode.specific,
            target_date=target or date.today(),
            window_start_hour=ws,
            window_start_minute=wsm,
            window_end_hour=we,
            raw=_extract_time_phrase(text) or (daypart or ""),
        )

    # Nothing time-like found -> treat as ASAP (earliest slot).
    return TimePref(mode=TimeMode.asap, raw="")


def _extract_time_phrase(text: str) -> str:
    low = text.lower()
    for cue in list(_ASAP_CUES) + list(_DAYPARTS):
        if cue in low:
            return cue
    m = re.search(r"\b\d{1,2}\s*(?::\s*\d{2})?\s*(am|pm)\b", low)
    return m.group(0) if m else ""


def _extract_location(text: str) -> str:
    """Grab the locality after 'in/near/at ...'. Falls back to trailing noun-ish tokens."""
    # e.g. "... in Koramangala tomorrow evening" -> "Koramangala"
    m = re.search(_LOC_PREP + r"\s+([A-Za-z0-9][\w\s,.-]{1,40}?)"
                  r"(?=\s+(?:tomorrow|today|tonight|this|next|on|at|by|asap|now|for|"
                  r"morning|noon|afternoon|evening|night|\d{1,2}\s*(?:am|pm))|[.,]|$)",
                  text, re.IGNORECASE)
    if m:
        loc = m.group(1).strip(" ,.-")
        # Drop a trailing service word accidentally captured.
        return loc
    return ""


def _extract_service(text: str, location: str) -> str:
    """Strip location + time cruft to leave the service phrase."""
    s = text
    if location:
        s = re.sub(re.escape(location), "", s, flags=re.IGNORECASE)
    # Remove location prepositions + time words.
    s = re.sub(_LOC_PREP + r"\s*", " ", s, flags=re.IGNORECASE)
    for w in list(_ASAP_CUES) + list(_DAYPARTS) + [
        "today", "tonight", "tomorrow", "this", "next", "book", "get me", "get",
        "i need", "need", "want", "please", "a ", "an ", "my ",
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    ]:
        s = re.sub(r"\b" + re.escape(w) + r"\b", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\b\d{1,2}\s*(?::\s*\d{2})?\s*(am|pm)\b", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"[.,]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" ,.-")
    return s


def heuristic_parse(text: str) -> ParsedNeed:
    location = _extract_location(text)
    time_pref = _detect_time(text)
    service = _extract_service(text, location) or text.strip()
    flat, loc = extract_flat(location)
    return ParsedNeed(
        service_need=service,
        location_text=loc,
        flat_number=flat,
        time_pref=time_pref,
        raw_text=text,
        parser_backend="heuristic",
    )


_API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
_MODEL_FILE = Path(__file__).parent.parent / ".uc_gemini_model"
_GOOD_MODEL: str | None = None      # last model that actually returned a result
_CANDIDATES: list[str] | None = None


def _score_model(name: str) -> float:
    s = 0.0
    if "flash" in name:
        s += 100                       # flash = fast + cheap, ideal for parsing
    if "lite" in name:
        s += 3                         # flash-lite is fine and less likely overloaded
    if "preview" in name or "exp" in name:
        s -= 30                        # prefer stable
    m = re.search(r"(\d+\.\d+)", name)
    if m:
        s += float(m.group(1)) * 10    # prefer newer version
    if name.endswith("-latest"):
        s += 2
    return s


def _load_good_model() -> str | None:
    global _GOOD_MODEL
    if _GOOD_MODEL:
        return _GOOD_MODEL
    try:
        if _MODEL_FILE.exists():
            _GOOD_MODEL = _MODEL_FILE.read_text().strip() or None
    except OSError:
        pass
    return _GOOD_MODEL


def _remember_good_model(model: str) -> None:
    global _GOOD_MODEL
    _GOOD_MODEL = model
    try:
        _MODEL_FILE.write_text(model)
    except OSError:
        pass


def _candidate_models() -> list[str]:
    """All generateContent-capable models this key can use, best first. Cached."""
    global _CANDIDATES
    if _CANDIDATES is not None:
        return _CANDIDATES
    try:
        with httpx.Client(timeout=15) as client:
            r = client.get(f"{_API_ROOT}/models", params={"key": settings.gemini_api_key})
            r.raise_for_status()
            models = r.json().get("models", [])
        usable = [m["name"].split("/")[-1] for m in models
                  if "generateContent" in m.get("supportedGenerationMethods", [])]
        usable.sort(key=_score_model, reverse=True)
        _CANDIDATES = usable
    except Exception:
        _CANDIDATES = []
    return _CANDIDATES


def _gemini_generate(prompt: str) -> str:
    """Call generateContent robustly: try the known-good/configured model first,
    retry transient 5xx/429 once, and on 404 or repeated failure fall through to
    the other models this key can use. Caches whichever model actually works."""
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}

    def call(model: str) -> str:
        with httpx.Client(timeout=25) as client:
            r = client.post(f"{_API_ROOT}/models/{model}:generateContent",
                            params={"key": settings.gemini_api_key}, json=body)
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]

    # Ordered, de-duplicated: known-good, configured, then discovered candidates.
    order: list[str] = []
    for m in (_load_good_model(), settings.gemini_model):
        if m and m not in order:
            order.append(m)
    for m in _candidate_models():
        if m not in order:
            order.append(m)

    last_err: Exception | None = None
    for model in order:
        for attempt in range(2):
            try:
                out = call(model)
                _remember_good_model(model)   # remember what actually works
                return out
            except httpx.HTTPStatusError as e:
                last_err = e
                code = e.response.status_code
                if code in (429, 500, 502, 503) and attempt == 0:
                    time.sleep(1.2)           # transient — retry the same model once
                    continue
                break                          # 404/4xx or exhausted — try next model
            except Exception as e:             # network/parse — next model
                last_err = e
                break
    raise last_err or RuntimeError("Gemini: no usable model for this key")


def _gemini_parse(text: str) -> ParsedNeed:
    """One Gemini call does BOTH jobs: extract {service, location, time} AND pick the
    right UC categoryKey from the live catalog. Falls back to heuristic on any error."""
    import json
    from datetime import date as _d

    from .catalog import CATEGORIES  # {categoryKey: (display, keywords)}

    cat_list = "\n".join(f"- {k}: {v[0]}" for k, v in CATEGORIES.items())
    today = _d.today().isoformat()
    prompt = (
        "You turn a home-services request into structured JSON for Urban Company.\n"
        f"Today's date is {today}.\n\n"
        "Pick the single best matching category KEY from this list "
        "(respect gender for salon/spa):\n" + cat_list + "\n\n"
        "Return ONLY JSON with these keys:\n"
        '  "service_need": short phrase of what they want,\n'
        '  "category_key": the best key from the list above,\n'
        '  "location_text": the address/area to geocode — INCLUDE the city and country '
        '(India unless clearly elsewhere) so it is unambiguous,\n'
        '  "time_mode": "asap" or "specific",\n'
        '  "date": "YYYY-MM-DD" for the requested day (resolve words like tomorrow / '
        'day after tomorrow / saturday relative to today) or null,\n'
        '  "window_start_hour": integer 0-23 or null,\n'
        '  "window_start_minute": integer 0-59 (the exact minute, e.g. 30 for "3:30pm", '
        '0 for a bare "3pm") or null,\n'
        '  "window_end_hour": integer 0-23 or null\n\n'
        f"User text: {text!r}"
    )

    raw = _gemini_generate(prompt)
    data = json.loads(raw)

    mode = TimeMode.asap if str(data.get("time_mode")) == "asap" else TimeMode.specific
    target = None
    if data.get("date"):
        try:
            target = date.fromisoformat(data["date"])
        except (ValueError, TypeError):
            target = None
    tp = TimePref(
        mode=mode if (target or mode == TimeMode.asap) else TimeMode.specific,
        target_date=target,
        window_start_hour=data.get("window_start_hour"),
        window_start_minute=data.get("window_start_minute"),
        window_end_hour=data.get("window_end_hour"),
        raw=str(data.get("time_mode") or ""),
    )
    ck = data.get("category_key")
    _flat, _loc = extract_flat(data.get("location_text") or "")
    from .catalog import CATEGORIES as _CATS
    return ParsedNeed(
        service_need=data.get("service_need") or text,
        location_text=_loc,
        flat_number=_flat,
        time_pref=tp,
        raw_text=text,
        parser_backend="gemini",
        category_key=ck if ck in _CATS else None,
        category_name=_CATS[ck][0] if ck in _CATS else None,
    )


def parse_text(text: str) -> ParsedNeed:
    if settings.effective_parser == "gemini" and settings.gemini_api_key:
        try:
            return _gemini_parse(text)
        except Exception as e:  # never break the spine on the LLM — fall back
            p = heuristic_parse(text)
            p.parser_note = (f"Gemini parse failed ({type(e).__name__}: {str(e)[:120]}); "
                             "used heuristic. Check GEMINI_API_KEY / GEMINI_MODEL.")
            return p
    return heuristic_parse(text)


def extract_flat(text: str) -> tuple[str | None, str]:
    """Split a flat/house/room number off the front of an address.
    '606, Stellar Heights Gachibowli' -> ('606', 'Stellar Heights Gachibowli').
    'Kondapur' -> (None, 'Kondapur'). Only splits when a place remains."""
    m = re.match(r"\s*(?:flat|room|no\.?|house|h\.?no\.?|#)?\s*"
                 r"([0-9]{1,5}[a-zA-Z]?(?:[-/][0-9a-zA-Z]+)?)\s*[,\-]?\s+(\S.*)",
                 text or "", re.I)
    if m and m.group(2).strip():
        return m.group(1), m.group(2).strip()
    return None, (text or "").strip()


def parse_structured(service_need: str, location_text: str, time_text: str) -> ParsedNeed:
    """When the UI sends 3 separate boxes, skip location/service extraction."""
    flat, loc = extract_flat(location_text)
    return ParsedNeed(
        service_need=service_need.strip(),
        location_text=loc,
        flat_number=flat,
        time_pref=_detect_time(time_text or ""),
        raw_text=f"{service_need} | {location_text} | {time_text}",
        parser_backend="structured",
    )
