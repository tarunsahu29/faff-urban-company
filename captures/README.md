# Recon — give me HAR files (this is the part only you can do)

The whole service already runs. Parse, fuzzy service-matching, and geocoding work
today. The missing pieces are a handful of **private Urban Company web endpoints**
that I can only see from your logged-in browser session. Instead of individual
cURLs, just record a **HAR per step** and drop them here — I'll extract the exact
requests + response shapes and wire them into `app/uc_client.py`.

## The auth model (why login is Step 1, not a bonus)
The agent mints its **own** session dynamically via OTP — no hardcoded cookie that
expires. Login is the first surface we capture and the first step of the pipeline:

```
request-otp (phone) → SMS to your phone → you type the OTP → verify-otp → fresh token
   → token cached in memory → used for location/catalog/slots/book → re-login on expiry
```

The only manual touch is typing the 6-digit code (inherent to OTP; reading SMS
automatically is out of scope + not good-citizen).

## How to record a HAR (Chrome / Edge)
1. `Cmd+Opt+I` → **Network** tab → filter **Fetch/XHR**.
2. ✅ Check **Preserve log**.  Click the 🚫 to clear the log right before each step.
3. Do the step's actions (below).
4. Right-click anywhere in the request list → **Save all as HAR with content**
   → save into this `captures/` folder with the exact filename given.

HARs embed session cookies, so `captures/*.har` is **gitignored** — never committed.
Don't scrub the auth headers (I need their names + to see where the token lands);
the file stays local and out of git.

---

## Record these, in order

### 1 — `01-login.har`  🎯 OTP login (capture from a LOGGED-OUT / incognito start)
**Start logged out** (use an Incognito window so the page bootstraps from scratch).
Open UC, DevTools recording already on, then:
- Land on the page (this captures any **guest/bootstrap token** fetched on load).
- Enter your **phone number** → continue  → the **request-otp** call fires (SMS sent).
- Enter the **OTP** from the SMS → the **verify-otp** call fires → you're logged in.

I need: the guest-bootstrap call (if any), request-otp (URL + body + headers like
device-id/app-version), and verify-otp (**where the session token lands** — body
field or set-cookie). This is what makes auth dynamic.

### 2 — `02-location.har`  🎯 location + geocode + serviceability
Still logged in, clear the log, then in the location/address box:
- **Type** a locality (e.g. "Koramangala") and let the **autocomplete suggestions** appear.
- **Click** a suggestion; let the page confirm it's serviceable.

Contains: UC **location autocomplete** (→ replaces Nominatim), **geocode/place-details**
(address → lat/lon/pincode), and **serviceability**.

### 3 — `03-catalog.har`  🎯 service catalog / search
Clear the log, then **search** a service (e.g. "bathroom cleaning") or drill a
category → sub-category → service; open its detail page so price/duration load.
I need each service's **id/SKU**, name, category, price, duration.

### 4 — `04-slots.har`  🎯 slot availability  ← finishes the MVP core
Clear the log, add that service, proceed to the **date & time slot picker**, switch a
couple of dates. I need slots: time + an opaque **slot id/ref** + availability flag.

---

## Bonus (later — only when we do the paid path)

### 5 — `05-book.har`  cart → checkout (STOP before paying)
Clear the log → add service + slot + address → advance through cart/checkout
**up to, but not including, the final payment/confirm**. Capture the
cart/checkout-setup calls. Don't complete payment.

---

## What I do with them
For each HAR I read the XHR requests + responses, fill the matching seam in
`app/uc_client.py` (URL + params + response mapping), and adjust the model field
names to the real JSON. Once `01-login.har` is wired the agent self-authenticates;
once `04-slots.har` is wired, `POST /slots` returns a real booking-ready UC slot.

> Tip: drop `01`–`04` at once and tell me — I'll wire login + the full read
> pipeline in one pass. (`01-login.har` first is the important one: it unlocks the
> session the others run under.)
