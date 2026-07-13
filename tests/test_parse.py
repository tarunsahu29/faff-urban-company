"""Smoke tests for the target-independent spine (parse + pick_slot).

Run: .venv/bin/python -m pytest tests/ -q     (or just execute this file)
"""
from datetime import date, timedelta

from app.parse import heuristic_parse, parse_structured
from app.models import TimeMode, TimePref, Slot
from app.slots import pick_slot


def test_deep_clean_tomorrow_evening():
    p = heuristic_parse("deep clean my 2BHK in Koramangala tomorrow evening")
    assert "clean" in p.service_need.lower()
    assert "koramangala" in p.location_text.lower()
    assert p.time_pref.mode == TimeMode.specific
    assert p.time_pref.window_start_hour == 17  # evening


def test_asap_near_hsr():
    p = heuristic_parse("need a bathroom cleaning ASAP near HSR Layout")
    assert "bathroom" in p.service_need.lower()
    assert "hsr" in p.location_text.lower()
    assert p.time_pref.mode == TimeMode.asap


def test_specific_clock():
    p = heuristic_parse("salon for women in Indiranagar this saturday at 3pm")
    assert "salon" in p.service_need.lower()
    assert p.time_pref.mode == TimeMode.specific
    assert p.time_pref.window_start_hour == 15  # 3pm


def test_structured_input():
    p = parse_structured("plumber", "HSR Layout", "asap")
    assert p.service_need == "plumber"
    assert p.location_text == "HSR Layout"
    assert p.time_pref.mode == TimeMode.asap


def test_pick_slot_asap_picks_earliest():
    today = date.today()
    slots = [
        Slot(slot_ref="b", date=today, start_hour=18, label="late"),
        Slot(slot_ref="a", date=today, start_hour=9, label="early"),
    ]
    chosen, alts = pick_slot(slots, TimePref(mode=TimeMode.asap))
    assert chosen.slot_ref == "a"
    assert len(alts) == 1


def test_pick_slot_specific_window():
    d = date.today() + timedelta(days=1)
    slots = [
        Slot(slot_ref="morning", date=d, start_hour=9, label="am"),
        Slot(slot_ref="evening", date=d, start_hour=18, label="pm"),
    ]
    pref = TimePref(mode=TimeMode.specific, target_date=d,
                    window_start_hour=17, window_end_hour=21)
    chosen, _ = pick_slot(slots, pref)
    assert chosen.slot_ref == "evening"


if __name__ == "__main__":
    import sys, traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    sys.exit(1 if failed else 0)
