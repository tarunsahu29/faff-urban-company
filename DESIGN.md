# Design

## Goal

Turn an unstructured request — a location, a service, and a time — into a
booking-ready Urban Company cart, and optionally place a real order. Urban Company
has no public API, so the system drives the same private web API the browser uses,
reconstructed from recorded traffic.

## Architecture

A single FastAPI process exposes a thin HTTP surface over a linear pipeline. Each
stage is an independent module with a narrow contract, so a stage can be tested or
replaced in isolation.

```
                 ┌── parse ──┐   ┌── catalog ──┐   ┌── geocode ──┐   ┌── journey ──┐
  request ──────▶│ free text │──▶│ resolve the │──▶│ coordinates │──▶│ draft cart: │──▶ result
                 │ -> intent │   │ UC service  │   │ + city      │   │ pkg+addr+   │
                 └───────────┘   └─────────────┘   └─────────────┘   │ slot        │
                                                                     └─────────────┘
```

| Module | Responsibility |
|--------|----------------|
| `parse.py` | Free text to `{service_need, location_text, flat, time_pref}`. An LLM call (Gemini) also picks the best category from the live catalog; a deterministic heuristic parser is the offline fallback. |
| `catalog.py` | Fuzzy, gender-aware resolution of the service phrase to a UC `categoryKey`. |
| `geocode.py` | UC's own location endpoint for coordinates, city key, and serviceability. |
| `slots.py` | Pure time-preference logic: minute-accurate matching, alternatives, exact-vs-nearest. |
| `journey.py` | The stateful booking journey: draft creation, package selection, address commit, slot selection, payment arming, order placement. |
| `browser_login.py` | Human-assisted login that passes Cloudflare Turnstile and captures the session token. |
| `uc_client.py` | Shared HTTP client: TLS/JA3 impersonation, required headers, retry/backoff. |
| `main.py` | FastAPI endpoints and orchestration. |

## Key decisions

**Reconstruct the journey, not individual calls.** Reaching a booking-ready cart is
a multi-step, stateful sequence on a server-side draft order — not one endpoint. The
journey module reproduces that exact sequence, keyed off a single draft the API
treats as the user's active cart.

**Ground-truth signals, never optimistic ones.** The system reports state only from
signals the server actually commits: an address is "set" only when the checkout
summary shows it selected; a slot is "ready" only when `updateSlot` returns
`checkoutState == ready_for_payment`; an order is "placed" only when a real
`checkoutOrderId` comes back. Transport-level success (`isError == false`) is never
treated as a booking on its own.

**Fast path for slot changes.** Switching only the time reuses the already configured
draft and re-runs a single `updateSlot` instead of the full journey, with a
consistency guard so a change to any input forces a full reconfigure — the preview can
never diverge from what an order would commit.

**Dynamic session.** Rather than a lifted cookie that expires, the app launches the
real browser login so the session is minted fresh; the human does the check and OTP
once and the token is captured for the API.

## Reliability and safety

- Real dispatch and payment are gated by `ALLOW_REAL_BOOKING` and a per-request
  `confirm`; the UI shows the exact address, slot, and amount before confirming.
- Failures are surfaced with the actual reason (for example, an account-level payment
  restriction) rather than a generic error.
- Rate limits are respected with backoff; the client impersonates Chrome to avoid TLS
  fingerprint blocks.
- Secrets are confined to `.env`; captures are gitignored and can be scrubbed with
  `scripts/redact_har.py`.

## Tech choices

FastAPI + Pydantic for a typed, minimal HTTP layer; `curl_cffi` for browser-grade TLS
impersonation; `patchright`/Chrome for a login that clears Turnstile; `rapidfuzz` and
`dateparser` for resolution and time parsing; Gemini for parsing with a
zero-dependency heuristic fallback.
