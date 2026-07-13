# Home Services Booking Automation — Urban Company

Given a location, a service need, and a time in plain English, this service resolves
the request against Urban Company's live catalog, reaches a fully configured,
booking-ready cart, and can place a real Cash-on-Delivery order end to end.

Example input: *"deep clean single bathroom, 201 Appala Abode Gachibowli, tomorrow 3pm"*.

## Pipeline

```
free text
   -> parse (LLM, heuristic fallback)          service need + address (+ flat) + time
   -> resolve service in the UC catalog        categoryKey
   -> geocode + serviceability (UC)            coordinates + city
   -> booking journey (UC private API)         draft cart: package + address + slot
   -> place order (Cash on Delivery)           confirmed booking
```

Login is dynamic: the app opens Urban Company's real login, the user completes the
human check and OTP once, and the resulting session token is captured for the API
calls. No hardcoded cookies.

## Run

```bash
python run.py
```

The first run creates a virtual environment, installs dependencies and the login
browser, copies `.env` from `.env.example`, starts the server on
`http://127.0.0.1:8137`, and opens it.

Flags: `--port <n>`, `--reinstall`, `--no-browser`, `--reload`
Tests: `.venv/bin/python -m tests.test_parse`

## Configuration (`.env`)

| Key | Purpose |
|-----|---------|
| `GEMINI_API_KEY` | Enables the LLM parser (free key from Google AI Studio). Without it, an offline heuristic parser is used. |
| `ALLOW_REAL_BOOKING` | Must be `true` — together with a per-request `confirm` flag — before any real order is placed. Default `false`. |

## API

- `POST /slots` — body `{"text": "..."}` or `{"service_need", "location_text", "time_text"}`.
  Configures the cart and returns the resolved service, location, selected slot,
  alternatives, and cart status.
- `POST /select_slot` — `{"slot_ref"}` switches the selected time on the already
  configured cart. No order is placed.
- `POST /book` — `{"service_need", "location_text", "time_text", "slot_ref", "confirm": true}`
  places the real Cash-on-Delivery order. Refused unless `confirm=true` **and**
  `ALLOW_REAL_BOOKING=true`.
- `POST /auth/browser-login`, `POST /auth/logout`, `GET /auth/status` — session.
- `GET /health` — parser backend and endpoint status.

## Safety

Reaching a booking-ready cart dispatches no professional and moves no money. Placing
a real order is gated by both `ALLOW_REAL_BOOKING` and a per-request `confirm`, and
the UI surfaces the exact address, slot, and amount before confirming. Secrets live
only in `.env` (gitignored); capture files are gitignored, and
`scripts/redact_har.py` scrubs tokens, cookies, and PII from any HAR.

## Layout

```
app/parse.py          free text -> structured intent
app/catalog.py        service resolution against the UC catalog
app/geocode.py        UC location + serviceability
app/slots.py          time-preference slot picking
app/journey.py        the stateful UC booking journey
app/browser_login.py  human-assisted login (Cloudflare Turnstile)
app/uc_client.py      HTTP client (curl_cffi, Chrome impersonation)
app/main.py           FastAPI endpoints
static/index.html     minimal UI
scripts/redact_har.py HAR secret scrubber
```

See [DESIGN.md](DESIGN.md) for the architecture and [TEARDOWN.md](TEARDOWN.md) for how
each target was solved.
