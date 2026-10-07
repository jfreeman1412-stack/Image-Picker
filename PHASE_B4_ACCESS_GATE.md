# Phase B.4 — Hand-off (Lightweight access gate on capture.sportslinephotography.com)

> **Status: decisions LOCKED 2026-05-26; build plan ready for pre-build review.**
> The gate mechanism is now chosen — **Option B (one shared password)**, which is
> the better fit for the three-tablet shared-door fleet (see Decision 2). Builds
> **second**, after C.3. Read this in full before starting.

## Why this exists

B.3 (Decision 5) put the capture app on a **stable public hostname** —
`capture.sportslinephotography.com`, a named `cloudflared` tunnel → the office
desktop's Vite server (`:8022`, which proxies `/api → :8020`). Stable + public
means **anyone with the URL can reach it**. B.4 adds a lightweight "keep the public
out" gate — a **shared door**, *not* per-volunteer accounts (that's a separate,
deferred feature; do **not** build account/login infrastructure here).

**The gate lives at the Cloudflare layer, in front of the tunnel** — no application
auth code is required for the gate itself. The only thing that *might* touch app
code is the optional offline re-auth backstop (Section 3), which you can skip if a
long session (below) is deemed sufficient.

**The hard constraint — the gate must NOT break the offline PWA.** The real
workflow is: **install at the office (online) → take the tablet off-grid for the
whole shoot → return and sync.** A tablet that authenticated at the office must
keep working offline *and* must be able to sync on return without a mid-shoot
re-auth. The bulk of this doc is making that true.

**Three tablets, not one.** Real shoots run **three tablets concurrently** — three
volunteers on three lines, each capturing a different portion of the same shoot's
roster on its own tablet. Each tablet installs and authenticates **independently**.
This is exactly why a **single shared password** (Decision 2) is the right gate:
one secret provisions all three with zero per-device setup, and the gate holds no
per-device state to collide on. (See also B.5 for the capture-side multi-tablet
handling and the concurrent-sync assessment.)

---

## How the offline PWA interacts with a front-door gate (the heart of B.4)

Recall the B.3 service-worker shape (`capture/vite.config.js`):
- Navigations are served from the **Workbox precache** (`navigateFallback:
  '/index.html'`) — so the **shell opens with no network**.
- `/api/*` is **`NetworkOnly`** — never cached; an offline `/api` call simply fails
  and the capture is queued.
- The install (and its IndexedDB queue) is **origin-bound** to
  `capture.sportslinephotography.com`.

Walking the workflow against a Cloudflare front-door gate:

| Moment | What happens | Verdict |
| --- | --- | --- |
| **Install at office (online)** | First navigation hits Cloudflare → gate challenge → authenticate → gate issues a **session credential** (cookie). SW precaches the shell. | ✅ normal |
| **Off-grid: open from home-screen icon** | Navigation served by the SW from cache — **never reaches Cloudflare**. No challenge. | ✅ shell opens offline |
| **Off-grid: capture all day** | `/api` PUTs fail (no network) → queued, exactly as B.3. Cloudflare never involved. | ✅ unaffected |
| **Return to office: auto-drain fires** | `/api` PUTs now reach the tunnel → **Cloudflare checks the session credential**. | ⚠️ **depends on whether the session is still valid** |

**The one real hazard is session expiry during the trip.** If the gate's session
outlived the off-grid span, the returning `/api` PUTs carry a still-valid credential
and sync normally. If it **expired**, Cloudflare answers the PUT with an **auth
challenge (a 302 redirect to a cross-origin login page), not a 200** — and a
background `fetch` **cannot complete an interactive login**. The drainer would see a
non-OK / opaque response and keep the items pending forever, **silently**. Worse:
because `navigateFallback` serves the **cached** shell, simply reopening the app may
**not** trigger a fresh top-level navigation to Cloudflare, so the volunteer might
never even see the login page.

**Two mitigations, in priority order:**

1. **Long session duration (primary, no app code).** Set the gate session to
   **longer than any realistic off-grid span and office-to-office gap** — e.g.
   **30 days**. Then the credential never expires between visits and the returning
   drain "just works." This alone satisfies the constraint for the dress-rehearsal
   workflow.
2. **Re-auth backstop in the app (optional, Section 3).** Detect an auth-challenge
   response shape on `/api` (a redirect to the gate / HTML body / 401-403 instead
   of JSON) and, instead of retrying forever, surface a clear **"Connection needs
   sign-in — tap to reconnect"** banner that forces a **top-level navigation** to a
   path on the **`navigateFallbackDenylist`** (so it bypasses the cached shell and
   actually hits Cloudflare), re-establishing the credential; then resume draining.

Both candidate mechanisms below share this same hazard and the same two
mitigations — the difference is how the credential is obtained and how long it
naturally lives.

---

## ⚠️ Decisions

### Decision 1 — Gate at the Cloudflare layer, gate the WHOLE hostname *(LOCKED)*

The gate sits in front of the tunnel and protects **every** path on
`capture.sportslinephotography.com` — both the shell **and** `/api`. Gating only the
HTML shell would leave `/api` (rosters, reference-status, uploads) open to anyone
who knows the URL shape. Whole-hostname is the Cloudflare default and the secure
choice.

### Decision 2 — Mechanism: a Cloudflare Worker enforcing ONE shared password *(LOCKED 2026-05-26)*

A tiny Worker (deployed in Cloudflare, **not** in this repo) bound to the hostname
checks a **signed cookie** against a **single shared secret**, set by a minimal
login page the Worker serves when the cookie is absent. This is the literal "one
shared password at the door."

**Why Option B wins for this fleet (no real downside vs Option A):** the workflow is
**three office-managed tablets sharing one door**. One shared secret provisions all
three with zero per-device setup; the gate keeps no per-device identity to manage.
Cloudflare Access's email-OTP model is per-identity — for three devices you'd need
either three allowlisted emails (drifting toward the per-volunteer accounts we
explicitly don't want) or an "any email + OTP" policy with a per-device email
round-trip. Both fight the shared-door goal. The things Access gives that B doesn't
— per-person revoke and an access audit log — don't matter for "keep the public
out" on a small managed fleet; if the secret leaks you rotate one value and re-auth
three tablets.

**Use a signed cookie, NOT raw HTTP Basic Auth.** Basic Auth would auto-resend
creds (nice), but browsers may **drop them on a full app restart**, after which a
background `/api` fetch gets a `401` it can't satisfy (the native dialog only fires
on a document load). A **signed cookie with a long `Max-Age` (Decision 3)** behaves
like a durable session, survives restarts, and is auto-sent on every `/api` request
including background drains — the best offline fit.

**Interaction nuance (shared by any mechanism):** if the cookie is ever missing or
expired, the Worker returns the login page / a redirect — which a background fetch
can't complete. Decision 3's long cookie makes that effectively never happen
between office visits; Section 3 is the optional backstop.

*Rejected — Option A (Cloudflare Access + email OTP):* official, managed, and
per-email-revocable, but its per-identity model is a poor fit for a one-door,
three-tablet fleet (see above).

### Decision 3 — Cookie `Max-Age` ≥ longest off-grid span (set 30 days) *(LOCKED)*

Set the signed cookie's `Max-Age` to **30 days**. The shoots are day-trips
returning to the office; 30 days guarantees the cookie never expires between office
visits, making the returning drain succeed without interaction. This is the single
most important offline mitigation.

### Decision 4 — Three tablets authenticate independently with the same password *(LOCKED)*

Each of the three tablets installs the PWA and signs in **independently** with the
**same shared password**, each receiving its **own** signed cookie. There is no
per-device gate state, so three independent installs/auths never collide or
interfere. This is the multi-tablet property the shared-password model gives for
free — verified explicitly in Section 2's checklist (all three installed, all three
syncing on return).

---

## Don't break

- **The offline shell, queue, and sync model are untouched.** B.4 adds no behavior
  to capture/enqueue/drain. The SW config changes only if you build Section 3
  (adding a re-auth path to `navigateFallbackDenylist`).
- **`/api` stays `NetworkOnly`.** Do not let the SW cache an auth-challenge response
  — that would poison the offline model. (It already won't; `/api` is NetworkOnly.)
- **No backend changes; no CORS changes.** The gate is entirely in Cloudflare; the
  browser still talks only to the one origin and Vite still proxies `/api → :8020`.
  `pytest -v` is untouched.
- **The install must not be orphaned.** The gate must not change the origin
  (scheme+host+port). It sits *in front of* the same hostname B.3 pinned.
- **Don't build accounts.** No per-volunteer identity, no login UI in the app, no
  user table. One shared door only.

---

## Build plan (each ends with a verified checkpoint; mostly Cloudflare config)

### Section 1 — Stand up the shared-password gate (Cloudflare Worker; no app code)
Deploy the Worker on `capture.sportslinephotography.com` (done **collaboratively**,
like the B.3 tunnel stand-up):
- Worker bound to the hostname, serving a minimal login page when the signed cookie
  is absent and setting a **signed cookie with 30-day `Max-Age`** on the correct
  password. Store the shared secret + signing key as Worker secrets.
- Confirm the gate covers `/` **and** `/api/*` (whole hostname, Decision 1).

**Manual check:** from a **fresh** browser (no cookie), hitting the URL shows the
login page; entering the password loads the capture app and a roster fetch
(`/api/...`) succeeds; the cookie persists across a reload. From an
un-authenticated client, **both** the shell and a direct `/api` request are blocked.

Commit (docs/config notes only): `phase B.4 section 1: shared-password worker gate on capture hostname`

### Section 2 — Verify the installed PWA is unaffected offline (no app code)
This is the acceptance core — almost entirely verification. Install the app online
(authenticating once), then exercise the full off-grid lifecycle.

**Manual check (on a real tablet):**
1. Install (Add to Home Screen) while online + authenticated.
2. Airplane mode → launch from the icon → **shell opens**, a cached shoot's roster
   reads, capture works, items queue as pending (all from cache — the gate is never
   consulted offline).
3. Close/reopen + device restart offline → queue + state intact (B.3 behavior).
4. Disable airplane mode (still within the session window) → **auto-drain succeeds**
   with no re-auth; badges flip green. **This proves the gate doesn't block sync on
   return.**

**Multi-tablet check (Decision 4 — run on all THREE tablets):** install + sign in
on each of the three tablets independently (same password); confirm each gets in
and runs offline on its own. Then bring all three back and let them drain **at the
same time** — every tablet authenticates and syncs without interfering with the
others (no shared gate state). (The backend side of three-at-once sync is assessed
in B.5's "Concurrent sync from three tablets" section.)

Commit (docs only): `phase B.4 section 2: verified offline PWA lifecycle behind the gate`

### Section 3 — Re-auth backstop *(OPTIONAL — build only if you want belt-and-suspenders beyond the 30-day session)*
Small app change so an **expired** session can never silently stall sync:
- In the drain's `/api` handling, detect an **auth-challenge response** (a redirect
  to the gate origin / an HTML body where JSON was expected / `401`/`403`) and
  classify it distinctly from network failure and from the existing 400/404 gate
  results — surface a **"Connection needs sign-in"** banner with a **Reconnect**
  action.
- **Reconnect** forces a top-level navigation to a dedicated path (e.g. `/signin`)
  added to **`navigateFallbackDenylist`** in `vite.config.js`, so it **bypasses the
  cached shell and hits Cloudflare**, re-establishing the credential; on return the
  normal drain triggers resume.
- Keep it lossless: queued items stay `pending` throughout (never dropped, never
  marked failed) — an auth challenge is a *transient* condition like being offline.

**Manual check:** set the gate session to a short duration (e.g. 5 min) to simulate
expiry; queue captures offline; let the session lapse; reconnect → the app shows
**Reconnect**, tapping it re-auths via a real navigation, and the queue then drains
to green with **no lost items**. (Restore the 30-day session afterward.)

Commit: `phase B.4 section 3: re-auth backstop for an expired gate session`

### Section 4 — README + adversarial checklist
Update `capture/README.md`: the gate (which mechanism, where it's configured, the
30-day session and why), the **install-while-authenticated** step, and an offline
checklist that explicitly includes **multi-day session validity** and (if built)
the **expired-session reconnect** path. State that the session-expiry item must be
run on a **target tablet** with the session shortened, since that's where the
SW/credential interaction is real.

Commit: `phase B.4 section 4: README + access-gate offline checklist`

---

## Acceptance

1. An un-authenticated visitor hitting `capture.sportslinephotography.com` is
   **blocked** at both `/` and `/api/*`.
2. After one authentication at the office, the app **installs and runs fully
   offline** — shell, roster cache, capture, queue — with the gate never consulted
   while off-grid.
3. On return within the session window, the queue **auto-drains with no re-auth**;
   references land identically to a normal online upload (B.3 acceptance #9 still
   holds).
4. The session lifetime (30 days) exceeds any realistic off-grid span, so expiry
   never occurs in the normal workflow.
5. *(If Section 3 built)* an expired session surfaces a **Reconnect** affordance and
   never silently stalls or drops queued items.
6. **Three tablets** install + authenticate independently with the **same shared
   password** and all three sync on return (concurrently) without interfering.
7. **No backend / CORS / `frontend/` changes;** `pytest -v` untouched and green; the
   SW still treats `/api` as NetworkOnly.

## Out of scope (defer — do NOT build in B.4)

- **Per-volunteer accounts / login / roles.** A separate, deferred feature.
- **Productionizing on the R640** — unchanged from B.3's scope line.
- **Rate-limiting / WAF rules / bot management** beyond the door gate.
- **Caching any `/api` or auth response in the service worker** — `/api` stays
  NetworkOnly by design.
- **Background Sync API** — drain still runs on B.3's foreground triggers.

## Must-verify-empirically (call out in the README)

Whether the browser on the **target tablet** keeps the signed cookie across a full
app restart and a multi-day gap (it should, with a 30-day `Max-Age`), and how the
Worker's login response behaves against a **background `/api` fetch** (vs a
top-level navigation), must be **confirmed on the actual device** — these are
platform specifics, not assumptions to ship on. Section 3's value depends on what
Section 2's testing reveals here.
