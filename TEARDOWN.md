# Teardown

Urban Company exposes no public API. The system drives the private web API the
browser uses, reconstructed from HAR captures of a real session. Each target below is
a distinct obstacle that had to be solved to move from free text to a placed order.

## 1. Authentication behind Cloudflare Turnstile

**Obstacle.** Login requires an `integrityToken` from a Cloudflare Turnstile challenge.
Driving the login with an automation framework failed the challenge even on a genuine
human click, because the automation flags (`--enable-automation`, `navigator.webdriver`)
are detectable.

**Crack.** Don't let the automation framework launch the browser. The app starts the
user's real Chrome as an ordinary process and merely attaches over the DevTools
protocol to observe traffic. Turnstile sees a clean browser; the human clears the
check and enters the OTP once; the session token is lifted from the login response.

## 2. Making the private API respond

**Obstacle.** Even with a valid token, the journey endpoints returned a "not live"
configuration and no draft order.

**Crack.** The web client sends a set of context headers the API depends on —
brand key, device and session identifiers, version codes, and critically a
`react-bundle-version` header. Without it the journey config resolves to an inactive
state. Replaying the full header set unlocked normal responses.

## 3. Free text to intent, and generic service resolution

**Obstacle.** Requests are messy ("clean my 1BHK bathroom tomorrow evening"), and the
target service must be one of Urban Company's many categories — not just the handful
seen in captures.

**Crack.** The full category list is pulled from UC's discovery screen and given to an
LLM, which extracts the structured intent and selects the best `categoryKey` in one
call (gender-aware for salon and spa). A deterministic fuzzy matcher over the same
catalog is the offline fallback. Flat/room numbers are split from the building so the
address is handled the way UC does.

## 4. Location and serviceability

**Obstacle.** The journey is keyed on a UC city and coordinates, not a raw address.

**Crack.** UC's own location endpoint resolves the typed place to coordinates, a city
key, and a place id, and reports whether it is serviceable — the same inputs the app
then feeds into the journey.

## 5. Reaching a booking-ready cart

**Obstacle.** A booking-ready cart is not one call. It is a stateful sequence over a
server-side draft order, where each step depends on the previous one's state and group
context.

**Crack.** Reproduce the sequence: create the draft, add the best-matching package,
advance to the checkout group, attach the address, load the slot grid, and select a
slot. The draft is the single source of state throughout, and group ids are read from
each response rather than assumed.

## 6. Committing the address to the draft

**Obstacle.** This was the hardest target. Seeding the draft with an address id at
creation time appeared to work but left the order without a committed, serviceable
address, so checkout failed. A captured order that seemed to prove the seed approach
had in fact reused an address committed in an earlier session.

**Crack.** Diffing the from-scratch captures against the one that placed showed the
real commit is a three-call sequence on the checkout group — activate the address,
revalidate the cart at the new location, then write the full address object into the
draft — not the creation-time seed. Correctness is then verified from the checkout
summary's address card, not from the presence of an id.

## 7. Selecting a slot and confirming it is real

**Obstacle.** The requested time is often unavailable, and an early version reported a
slot as "selected" from a UI text field that did not reflect the committed state.

**Crack.** Slots are matched to the exact requested minute, with nearest alternatives
surfaced when the exact time is taken. A slot counts as committed only when
`updateSlot` returns `checkoutState == ready_for_payment` and the availability check
passes — an authoritative signal rather than a rendered label.

## 8. Placing the real order

**Obstacle.** The order call initially returned a transport-level success but created
no order. Two things were missing.

**Crack.** First, the checkout must be "armed": the payment-options call registers a
payment flow for the draft, without which the order has no payment context. Second,
success is defined only by a real `checkoutOrderId` in the response, not by the
absence of a transport error. With both in place, a Cash-on-Delivery order places and
returns its order id. Where the order is refused for an account-level reason (for
example a temporary payment-mode restriction), that specific reason is read from the
payment-options response and surfaced to the user.
