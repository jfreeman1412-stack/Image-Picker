# Phase 5 — Hand-off to Claude Code (Tier 1 review-speed bundle)

Read this in full before starting. This phase is the WISHLIST "Tier 1 — UX
speed wins": three keyboard/visual changes that make plowing through a team's
clusters fast. They compound — 5.1 + 5.2 together let the user clear a team
without touching the mouse.

This is **almost entirely frontend**. One small optional backend touch is
called out in Section 3. Run `pytest -v` from `backend/` after any backend
change and at the end; all 172 existing tests must keep passing.

## Context you need

- The cluster review screen is `frontend/src/pages/SessionDetail.jsx`. It
  renders one `frontend/src/components/ClusterCard.jsx` per cluster, each with
  a `.thumb` per image (draggable; click opens the preview modal).
- The full-size preview is `frontend/src/components/ImageModal.jsx`, opened by
  `SessionDetail`'s `previewImage` state (set from `onPreview(img)`), closed on
  ESC/backdrop. It currently shows a single image with no navigation.
- Roles are assigned today via the per-thumb badge popover (TEAM / PANO / IND /
  BUDDY / REJECTED / reset) — handler `onSetRole(image_id, cluster_id, role)` →
  `POST /api/sessions/{id}/set-role`, and `onClearOverride(...)` →
  `POST /api/sessions/{id}/clear-role-override`. Both already exist and work;
  do NOT change the endpoints.
- A cluster card already computes `visibleReasons`, `isCoach`, etc. Role values:
  `team | panoramic | individual | buddy | rejected`. `rejected` is manual-only.
- `is_coach_for_sort` is on each cluster in the `/clusters` payload.
- Existing keyboard handling: `SessionDetail` has a window `keydown` listener
  for `R` (mark reviewed & next). Any new global key handling must follow the
  same guard it uses: ignore when typing in an input/textarea/contenteditable,
  and ignore with modifier keys.

Don't break: drag-and-drop reassignment, the review action bar / `R` shortcut,
the validation gate, flag visibility, the preview modal's existing
ESC/backdrop close.

---

## Section 1 — Image modal: arrow-key navigation (WISHLIST 1.1)

When the full-size modal is open:

- `←` / `→` navigate to the previous / next image **within the same cluster**,
  in the order they're displayed on that cluster card.
- **Stop at the ends** (no wrap). At the first image `←` does nothing; at the
  last `→` does nothing. (Clearer mental model than wrap — per the wishlist.)
- ESC still closes (existing behavior, unchanged).
- The modal shows its position + filename, e.g. `Image 4 of 12 · IMG_4521.jpg`.

### Implementation notes

- The modal currently receives a single image. It needs the **list** of images
  for the cluster the opened image belongs to, plus the current index. Cleanest:
  change `SessionDetail`'s preview state from a single image to
  `{ images: [...], index: N }`, and have `ClusterCard`'s thumbnail `onPreview`
  pass the cluster's image array + the clicked image's index. `ImageModal`
  takes `images` + `index` + `onIndexChange` and renders
  `images[index]`.
- Keyboard listener lives in `ImageModal` (only active while open), added/removed
  in the same `useEffect` that already handles ESC. `←`/`→` call
  `onIndexChange(clamp(index ± 1, 0, images.length - 1))`.
- Title line: `Image {index+1} of {images.length} · {filename}` above or over
  the image.
- Preload niceness (optional, nice-to-have): set `src` of the next/prev image
  in a hidden `<img>` so navigation feels instant. Skip if it complicates.

### Tests

Frontend has no test runner configured, so this is manual-acceptance:
- Open an image mid-cluster, arrow both directions, confirm it stops at ends,
  ESC still closes, title updates.

---

## Section 2 — Single-key role assignment (WISHLIST 1.2)

While the **modal is open**, single keys set the role of the currently shown
image and auto-advance to the next image in the cluster:

| Key | Role |
|-----|------|
| `T` | team |
| `P` | panoramic |
| `I` | individual |
| `B` | buddy |
| `X` | rejected |
| `U` | clear manual override (reset to auto) |

- After a role key, **auto-advance** to the next image (same clamp rule as
  Section 1 — at the last image, stay put). `Shift+<key>` assigns **without**
  advancing (for when you want to fix one and stay).
- `U` calls the existing clear-override path, not set-role.
- Reuse the existing handlers: `onSetRole(image_id, cluster_id, role)` and
  `onClearOverride(image_id, cluster_id)`. The modal needs the
  `cluster_id` the current image belongs to — pass it through with the image
  list (e.g. `{ cluster_id, images, index }`).
- These handlers currently trigger a full `load()` in `SessionDetail` (refetch
  + re-render). That's fine, but it will re-create the cluster list and could
  desync the modal's `images`/`index`. Handle this: after a role change, keep
  the modal open on the same image position. Simplest robust approach — the
  modal keeps its own working copy of the image list it was opened with and
  tracks role locally for display; or `SessionDetail` re-derives the modal's
  image list from refreshed `clusters` by `cluster_id` and preserves `index`.
  Pick whichever is least fragile and write a one-line comment explaining why.
- **Also wire the same keys on the cluster grid** when a thumbnail is
  "focused/selected" (per the wishlist). Minimum viable: clicking a thumbnail
  once selects it (visible outline) without opening the modal is a bigger
  change — instead, the acceptable scope here is: the keys work in the modal
  (primary flow). If selecting-on-grid is non-trivial, note it as deferred in
  `HANDOFF.md` rather than half-building it.
- Respect the existing typing guard (don't fire while renaming a cluster, etc.).
- Don't collide with the existing `R` shortcut (different screen state — modal
  open vs not; if both could be active, modal keys take precedence while open).

### Cheatsheet

Add an unobtrusive `?` affordance (small button or "keys" link) in the modal
that toggles a small legend listing the shortcuts. Plain React state, no lib.

### Tests

Manual-acceptance: open modal, press T/P/I/B/X — badge changes, advances;
Shift+key stays; U clears an overridden image; `?` shows the legend.

---

## Section 3 — Visual "complete" state for player cards (WISHLIST 1.3)

A cluster card should signal at a glance whether it still needs attention,
**distinct from the per-session `reviewed` state** (which is per team, not per
cluster).

Per-cluster state:

- **Player cluster** (`is_coach_for_sort == false`): "complete" iff it has both
  a `team` AND a `panoramic` role assigned. Otherwise "incomplete".
- **Coach cluster** (`is_coach_for_sort == true`): "complete" iff it has a
  `team` role. Pano not required.

Visual treatment on `ClusterCard`:

- Complete → green left border / subtle green tint + a small ✓.
- Incomplete → amber/red border (reuse the existing `.review` warn styling or a
  sibling class). Don't double up confusingly with the existing
  `visible_review_reasons` warn state — if a card is both flagged and
  incomplete, incomplete/▲ should read clearly. Use your judgment; keep it
  scannable, not noisy.

### Where the data comes from

The backend **already computes exactly this** —
`_compute_review_readiness()` in `backend/app/api/sessions.py` returns
`incomplete_clusters: [{cluster_id, label, missing:[...]}]`, and
`SessionDetail` already fetches `/review-readiness` into `readiness` state and
builds `incompleteClusterIds`. So:

- Pass an `incomplete` boolean (and optionally `missing: ["pano"]`) down to each
  `ClusterCard` from `SessionDetail` using the existing `incompleteClusterIds`
  set. **No new endpoint, no backend change required** for the common path.
- Optional backend nicety (only if it makes the UI cleaner): add a
  `complete: bool` to each cluster in the `/clusters` payload computed the same
  way as readiness. If you do this, add a backend test mirroring
  `test_review_readiness.py`'s per-cluster logic and keep all existing tests
  green. If it adds no real value over the existing readiness set, skip it and
  say so.

### Tests

If you add the backend `complete` field: `pytest` test for player-needs-both /
coach-needs-team. Otherwise manual-acceptance: a cluster missing pano shows the
incomplete style; assigning a pano flips it to complete without a reload feeling
broken.

---

## Conventions (unchanged from prior phases)

- No new frontend dependencies — plain React state, matching the existing
  hand-rolled modal/popover/toast components.
- Don't modify `set-role` / `clear-role-override` / `review-readiness`
  endpoints or sort logic.
- Keyboard handlers must respect the existing input-typing guard.
- `pytest -v` from `backend/` stays green (172 tests) after any backend touch.
- Single-line git commit messages; commit only when the user asks.
- Update `HANDOFF.md` with anything deferred (e.g. grid-selection keys if not
  built).

## Acceptance

1. Open a thumbnail → modal shows "Image N of M · filename"; ←/→ move within
   the cluster and stop at the ends; ESC closes.
2. With the modal open, T/P/I/B/X set the role and advance; Shift+key stays;
   U resets an overridden image; `?` shows a shortcut legend.
3. Cluster cards visibly distinguish complete (team+pano, or coach+team) from
   incomplete, separate from the per-team reviewed pill, and update when roles
   change.
4. Drag-and-drop, the `R` review flow, the validation gate, flag visibility,
   and the preview modal's ESC/backdrop close all still work.
5. `pytest -v` passes (≥172).
