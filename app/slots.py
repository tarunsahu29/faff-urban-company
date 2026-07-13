"""Slot availability + pick-slot logic — the heart of the MVP.

`normalize_slots` + `pick_slot` are pure functions (unit-testable without any
network). `fetch_slots` calls the live UC endpoint once wired; until then it
raises NeedsCapture and the API reports status "needs_capture" honestly rather
than inventing slots.
"""
from __future__ import annotations

from datetime import date as _date
from datetime import datetime, timedelta

from .models import Slot, TimeMode, TimePref
from .uc_client import uc

# How many days forward to scan when the requested day has no slots (or ASAP).
_SCAN_DAYS = 7


def normalize_slots(raw_slots: list[dict], on_date: _date) -> list[Slot]:
    """Map UC's raw slot objects -> our Slot model.

    TODO(recon): adjust field names to the real response shape. The keys below
    are guesses; fix them against the captured JSON. Kept defensive so partial
    matches still yield usable slots.
    """
    out: list[Slot] = []
    for s in raw_slots:
        # Guess common shapes: {"time":"17:00","available":true,"slotId":"..."} etc.
        ref = str(s.get("slotId") or s.get("id") or s.get("slot_ref")
                  or s.get("time") or "")
        hour = s.get("hour")
        if hour is None:
            t = str(s.get("time") or s.get("startTime") or "")
            if ":" in t:
                try:
                    hour = int(t.split(":")[0])
                except ValueError:
                    hour = None
        minute = 0
        t = str(s.get("time") or s.get("startTime") or "")
        if ":" in t:
            try:
                minute = int(t.split(":")[1][:2])
            except (ValueError, IndexError):
                minute = 0
        available = s.get("available", s.get("isAvailable", True))
        label = s.get("label") or s.get("display") or (
            f"{on_date.isoformat()} {hour:02d}:{minute:02d}" if hour is not None else ref)
        out.append(Slot(
            slot_ref=ref,
            date=on_date,
            start_hour=hour if hour is not None else 0,
            start_minute=minute,
            label=label,
            available=bool(available),
            raw=s,
        ))
    return out


def _slot_minutes(s: Slot) -> int:
    return s.start_hour * 60 + s.start_minute


def _requested_minutes(pref: TimePref) -> int | None:
    """The exact minute-of-day the user asked for. An explicit clock ("3pm"/"3:30pm")
    aims at that precise minute; a vague daypart ("evening") aims at the window's
    midpoint. None => no time constraint."""
    if pref.window_start_hour is None:
        return None
    if pref.window_start_minute is not None:            # explicit clock
        return pref.window_start_hour * 60 + pref.window_start_minute
    end_h = pref.window_end_hour or pref.window_start_hour  # daypart -> midpoint
    return int(((pref.window_start_hour + end_h) / 2) * 60)


def pick_slot(slots: list[Slot], pref: TimePref) -> tuple[Slot | None, list[Slot], bool]:
    """Choose the best slot for the time preference.

    Returns (chosen, alternatives, exact_match) where exact_match is True when the
    chosen slot is exactly what the user asked for (the precise minute for a clock
    request, in-window for a daypart, or the earliest for ASAP). Pure function.
    """
    available = [s for s in slots if s.available]
    if not available:
        return None, [], False

    available.sort(key=lambda s: (s.date, s.start_hour, s.start_minute))

    if pref.mode == TimeMode.asap:
        chosen = available[0]  # earliest available — exactly what "ASAP" asked for
        return chosen, [s for s in available if s is not chosen][:5], True

    requested = _requested_minutes(pref)
    explicit_clock = pref.window_start_minute is not None

    def score(s: Slot) -> tuple:
        date_gap = abs((s.date - (pref.target_date or s.date)).days)
        in_window = 0
        if pref.window_start_hour is not None and pref.window_end_hour is not None:
            in_window = 0 if pref.window_start_hour <= s.start_hour <= pref.window_end_hour else 1
        minute_gap = abs(_slot_minutes(s) - requested) if requested is not None else 0
        return (date_gap, in_window, minute_gap)

    ranked = sorted(available, key=score)
    chosen = ranked[0]
    on_day = chosen.date == (pref.target_date or chosen.date)
    if requested is None:
        exact = True
    elif explicit_clock:                       # asked for a precise time
        exact = on_day and _slot_minutes(chosen) == requested
    else:                                       # asked for a daypart
        in_win = (pref.window_end_hour is not None
                  and pref.window_start_hour <= chosen.start_hour <= pref.window_end_hour)
        exact = on_day and bool(in_win)
    return chosen, [s for s in ranked if s is not chosen][:5], exact


# The full journey (initiateJourney -> package -> address -> slot page -> select
# -> verify) lives in journey.build_booking; main calls it directly. pick_slot above
# is the pure time-preference picker it uses.
