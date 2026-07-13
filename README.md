# faff — Home Services Booking Automation (Urban Company)

> **Brief:** *"Given a location + service need + time, reach a booking-ready slot.
> Bonus: actually book it programmatically."*

A FastAPI service that takes free text like *"deep clean my 2BHK in Koramangala
tomorrow evening"* and drives it to a concrete, booking-ready Urban Company slot.

## The spine

```
  login (phone → OTP → fresh token)   ◀── step 1, dynamic session, no hardcoded cookie
                          │
free text  ──parse──▶  {service_need, location_text, time_pref}
                          │
              resolve service (fuzzy match → UC SKU)
                          │
              geocode + serviceability (UC location endpoint, Nominatim fallback)
                          │
              slot availability (UC) ──pick──▶  BOOKING-READY SLOT   ◀── core done
                          │
              (bonus) assemble → confirm → pay   (real pro, real money)
```

The agent mints its own UC session via OTP — no lifted browser cookie to expire.
The one manual touch is typing the SMS code; everything else is programmatic and
the session auto-refreshes by re-logging-in on expiry.

## Status

| Stage | State |
|-------|-------|
| **OTP login (dynamic session)** — step 1 | ⏳ needs `01-login.har` |
| Free-text parse (heuristic, no key; optional Gemini/OpenAI) | ✅ working |
| Service resolution (fuzzy match) | ✅ working (placeholder catalog until capture) |
| Geocode — UC location endpoint (primary) + Nominatim fallback | ✅ fallback working; UC ⏳ needs capture |
| Serviceability (UC) | ⏳ needs capture |
| Slot availability (UC) — **MVP core** | ⏳ needs capture |
| Book (bonus) | ⏳ needs capture |

"Needs capture" = a private UC web endpoint that must be grabbed from a logged-in
browser session. See **[captures/README.md](captures/README.md)** — that's the one
part that needs you. Until wired, the API degrades gracefully to `status: needs_capture`
so the rest of the spine is fully demoable.

## Run

```bash
python run.py        # creates .venv, installs deps, copies .env, launches, opens browser
```

That's the whole thing. Flags: `--port 8137`, `--reinstall`, `--no-browser`, `--reload`.

Tests: `.venv/bin/python -m tests.test_parse`

## API

- `POST /slots` — body `{"text": "..."}` **or** `{"service_need","location_text","time_text"}`.
  Returns `{parsed, service, location, booking_ready_slot, alternatives, status, notes}`.
- `POST /book` — (bonus) `{"slot_ref","service_id","confirm":true}`. Guarded: refuses
  without `confirm=true`, and errors until the booking endpoint is wired.
- `GET /health` — shows parser backend + which UC endpoints are wired.

## Design notes

- **UC client is isolated** (`app/uc_client.py`) so the harness could retarget a 4th
  app. Built on `curl_cffi` with Chrome impersonation to defeat TLS/JA3 fingerprinting;
  backs off on `429`; polite delay between calls.
- **Secrets only in env** (`.env`, gitignored). Captures with tokens are gitignored too.
- **Safety:** reaching a booking-ready slot dispatches nothing and costs nothing. The
  paid bonus is double-guarded and, if attempted, uses the cheapest service + earliest
  slot, ready to cancel, funded from the ₹1,000 reimbursement — never looped.
- **Judgment surfaces** are surfaced, not hidden: fuzzy match returns runner-up
  candidates + a score; time preference resolves to ASAP-vs-specific with a window; a
  missing slot is flagged rather than faked.
