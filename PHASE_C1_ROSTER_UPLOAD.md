# Phase C.1 — Hand-off (Roster Upload: CSV → column mapping → player roster)

> **Status: DRAFT for review (written 2026-05-24). Do NOT build yet.** This doc
> reflects the four locked decisions below; review it and the section plan before
> any code is written.

Read this in full before starting.

## Why this is "C.1" (the phase name)

The project has three tracks: **A-series** = the reference-photo *backend* spine
(A.1 roster model, A.2 reference upload, A.3 matching, A.4 pipeline integration);
**B-series** = the *mobile capture* app (B.1 prototype, B.2 check-in tool). This
feature is neither: it's **desktop operator tooling** — a real UI in the sorting
app for loading a shoot's player roster from an arbitrary CSV. That's a new,
coherent track, so it opens the **C-series**. (If you'd rather file it as **A.5**
because it feeds the A.1 model, the content is identical — only the filename
changes. C.1 is recommended.)

## What this feature does

During job setup in the **desktop sorting app**, an operator loads a shoot's
roster from a CSV. Because rosters arrive in many different column layouts, the
operator **maps the CSV's columns** to the app's canonical fields; the app
**validates strictly**; on success the roster is written for that job. Rosters
always arrive **before the shoot** and are attached **whenever the CSV shows up**
(same day or later) — the job is created first, the roster attached after, and
attaching is **optional/deferred**, never required at job creation.

This writes into the **A.1 `PlayerMembership` model** (the reference/capture
spine) via the existing load core — it is the first desktop UI for that roster,
which until now has been curl/test-seeded only.

---

## ⚠️ Read this first — there are TWO roster systems, and they share a CSV format

Easy to confuse; getting this wrong wastes the whole effort:

| | **Phase 6 `RosterEntry`** (cross-check) | **A.1 `PlayerMembership`** (this feature's target) |
|---|---|---|
| Endpoint | `POST /api/jobs/{job_id}/roster` | `POST /api/players/roster/{job_id}` |
| Purpose | Flag wrong-team clusters *after* the pipeline runs | Identity spine the B-series capture app reads |
| Coach flag? | **No** | **Yes** — `is_coach`, from a `Coach-` name prefix |
| Desktop UI today | **Yes** — wizard "Roster CSV" step + `RosterModal` | **None** (curl-seeded) |

**This feature targets the A.1 `PlayerMembership` model only** (Decision 4). The
coach flag lives only there, which is why "preserve the coach behavior" pins the
target. The Phase 6 cross-check roster is **left exactly as-is**; we only add
clear UI labels so an operator isn't confused into thinking they uploaded "the
roster" when they used the other one (Section 7).

**The existing A.1 ingest path you are reusing** (`services/players.py`):

`decode_bytes` → `parse_csv` (a **headerless, strictly-2-column positional**
parser; col0=name, col1=team; blank/short/over-column rows are *silently skipped*
and counted) → `replace_shoot_memberships(db, job_id, rows)`, which **wipes the
job's memberships, upserts global `Player` rows by normalized name, inserts fresh
memberships, and derives `is_coach` from the `Coach-` prefix**. `rows` is a list
of `(raw_name, team_name)` tuples.

The C.1 feature **does not change any of that.** It adds a *header-aware,
arbitrary-column* parse + a *strict* validator in front, transforms mapped rows
into the same `(name, team)` tuples, and calls the **unchanged**
`replace_shoot_memberships` core. The positional `POST /api/players/roster/{job_id}`
endpoint and its tests stay **byte-for-byte** (the B.2 seed recipe still curls it).

---

## ⚠️ Decisions — LOCKED 2026-05-24

### Decision 1 — Backend shape: a **new mapping-aware endpoint** *(LOCKED)*

Mapping/validation lives **server-side** in a new additive endpoint pair, reusing
the A.1 load core. Rejected: *client-side transform* (puts strict validation in
untested JS — `frontend/` has no JS test runner) and *extending the existing
endpoint* (mutates a frozen A.1 contract the seed recipe depends on).

### Decision 2 — Saved mapping templates: **design-for-later, don't build now** *(LOCKED)*

Ship per-upload mapping now. The mapping is a **standalone serializable object**
(spec below) so a "save/re-apply named mapping" feature drops in cleanly as a
later phase/section once repeat-client layouts are confirmed. No templates table,
endpoints, or UI in C.1.

### Decision 3 — Jersey number: **dropped from scope** *(LOCKED — supersedes the earlier "jersey optional" note)*

Canonical fields are **name + team, full stop.** Jersey is **not** mappable and
is **ignored even if present** in the CSV. Consequence: **no schema change and no
migration anywhere in C.1** — `PlayerMembership` is untouched.

### Decision 4 — A.1 player roster only; flag coexistence *(LOCKED)*

The mapped upload writes **only** `PlayerMembership` (A.1). The Phase 6
cross-check roster (`RosterEntry`) is **not** written and **not** modified. UI
labels make the two distinct (Section 7). *Dual-write* and *replacing the wizard's
positional step* were considered and rejected for scope.

---

## Canonical fields & the mapping spec

**Canonical fields (the only two):**
- **name** — *required.* Two mapping modes:
  - **full** — one column holds the whole name (e.g. `"Eleanor-Pederson"`).
  - **split** — separate first-name and last-name columns.
- **team** — *required.* One column.

**Coaches** are unchanged from current ingest: after the name is assembled,
`is_coach` is derived from the `Coach-` prefix on that assembled name (the
existing `is_coach_name`). There is **no** separate "role/coach" column to map —
a coach row carries the `Coach-` prefix in its name, exactly as today.

**Parent contact / other PII is ignored entirely** — unmapped columns are never
read, never stored.

### The mapping object (serializable; template-ready)

```jsonc
{
  "has_header": true,            // is row 1 a header row?
  "name_mode": "full",           // "full" | "split"
  "name_column":  "Player Name", // when name_mode == "full"
  "first_name_column": "First",  // when name_mode == "split"
  "last_name_column":  "Last",   // when name_mode == "split"
  "team_column": "Team"          // always
}
```

- Columns are referenced by **header name** when `has_header` is true (robust and
  the stable thing a repeat client keeps); by **positional label** (`"Column 1"`,
  `"Column 2"`, …, which the inspect endpoint emits) when `has_header` is false.
- Keeping this a flat, named object — not positional indices baked into code — is
  exactly what makes Decision 2's future templates a drop-in.

### Name assembly

- **full:** `name = row[name_column].strip()`.
- **split:** join the non-empty, trimmed `[first, last]` parts with a single
  space. **A row with only a last name (or only a first) is still a valid name.**
- `team = row[team_column].strip()`.
- The assembled `(name, team)` tuple is what feeds `replace_shoot_memberships`,
  which then normalizes and dedups. **`norm_name` strips all separators**, so the
  join character ("Eleanor Pederson" vs "Eleanor-Pederson") is cosmetic and does
  **not** affect identity or A.4 matching against EXIF copyright tags. (Join with
  a space; trivially flippable.)

---

## Validation rules (strict — block the whole upload, name the rows)

Two layers, both server-side and pytest-tested:

**1. Mapping completeness (pre-flight).** Reject before reading rows if:
- `team_column` is unmapped, OR
- `name_mode == "full"` and `name_column` is unmapped, OR
- `name_mode == "split"` and **both** first/last columns are unmapped.

**2. Per-row required data.** For every data row, collect problems by reason:
- `missing_name` — full mode name cell empty; or split mode **both** first and
  last empty.
- `missing_team` — team cell empty.

**If the mapping is incomplete OR any row has a problem, the entire upload is
blocked** and the response names the specific rows and reason — e.g.
`"3 rows missing a team: rows 12, 34, 51"` — so the operator can fix the CSV
fast. **Row numbers are 1-based file line numbers including the header row** (so
they match what the operator sees in Excel: with a header, the first data row is
row 2).

A row with a name and a team is **valid** — coaches included (a coach is just a
valid row whose name triggers the `Coach-` flag).

### Re-upload = full replace, with a captured-references guard

Re-uploading **replaces the entire roster** for the job (the core already does
wipe-and-reload). Rosters load before the shoot, so captures essentially never
exist yet — the guard is invisible in the normal case. It fires only in the rare
edge case where **reference photos were already captured for this shoot**
(`ReferenceFace WHERE captured_job_id == job_id` — the exact query the B.2
`reference-status` endpoint already uses, `api/players.py:91`). On commit, if that
count is > 0 and the caller hasn't confirmed, return **409** with the count so the
UI can warn ("N players already have photos captured for this shoot; replacing the
roster may change which player those captures line up with"). Replacing
memberships does **not** delete the references themselves (they cascade from the
global `Player`, not the membership) — the guard is purely a heads-up. Keep it
simple: count + confirm, mirroring the `run-all` 409-then-confirm pattern
(`api/jobs.py:496`).

---

## API surface (additive — A.1/A.2 stay frozen)

Two new routes in `api/players.py`, declared in the `/roster/...` group **before**
the dynamic `/{player_id}` route (FastAPI match order — same rule the file already
follows). Both 3+ segments, so no collision with the existing
`POST/GET/DELETE /roster/{job_id}` or `/roster/{job_id}/reference-status`.

```
POST /api/players/roster/{job_id}/inspect
     multipart file → { columns: [str], sample_rows: [[str]], total_rows: int }
     Lets the UI build the mapping dropdowns + a small preview. 404 if job missing.

POST /api/players/roster/{job_id}/mapped
     multipart file + Form: mapping (JSON string), dry_run (bool), confirm_replace (bool)
       dry_run=true  → validate only, write nothing; returns the full report.
       dry_run=false → re-validate (authoritative); on invalid → 400 report;
                       if captured refs exist and !confirm_replace → 409;
                       else replace_shoot_memberships + commit → summary.
     400 on bad mapping JSON or failed validation; 404 if job missing.
```

> One endpoint does both validate-preview and commit via `dry_run`, mirroring how
> `run-all` carries its destructive guard on a single endpoint with a `force`
> flag. If on review you'd prefer a separate read-only `/validate` route, it's a
> clean split — but commit must **always re-validate** regardless (never trust a
> stale client validation).

**Validation report body** (returned by `dry_run`, and as the 400 `detail` on a
blocked commit):

```jsonc
{
  "ok": false,
  "error": "roster_validation",                 // present when ok=false
  "mapping_errors": ["team column not mapped"], // empty when mapping is complete
  "row_errors": [
    { "reason": "missing_team", "label": "missing a team", "rows": [12, 34, 51] },
    { "reason": "missing_name", "label": "missing a name", "rows": [7] }
  ],
  "summary": { "valid_rows": 44, "invalid_rows": 4, "distinct_teams": 3, "coaches": 3 },
  "preview": [ { "name": "Ava-Nguyen", "team": "Lions", "is_coach": false } ],  // first ~10 valid
  "references_warning": { "count": 5, "message": "5 players already have reference photos captured for this shoot." }
}
```

On a clean `dry_run`: `ok: true`, empty `row_errors`/`mapping_errors`, populated
`summary`/`preview`/`references_warning`. On a successful commit: the
`replace_shoot_memberships` summary dict (already returns `players_created`,
`memberships_loaded`, `coaches`, `distinct_teams`, `entries_skipped`, …).
Captured-refs block on commit: **409** `detail = { error: "references_exist",
count, message }`.

---

## Don't break

- **A.1 and A.2 stay frozen.** No edits to `replace_shoot_memberships`,
  `load_shoot_roster_from_text`, `upsert_player`, `is_coach_name`, the positional
  `POST/GET/DELETE /api/players/roster/{job_id}`, or `services/references.py`. Add
  new functions/routes alongside.
- **Phase 6 cross-check roster is untouched** (Decision 4) — no writes to
  `RosterEntry`, no behavior change to `/api/jobs/{job_id}/roster` or
  `RosterModal`. Section 7's labeling is **text/tooltip only**, no logic.
- **No schema change / migration** (Decision 3) — `PlayerMembership` is untouched;
  don't add to `_PHASE2_COLUMNS`.
- **All backend tests stay green.** Record the current `pytest -v` baseline before
  starting (it was 449 as of B.2 §1 and grew with B.2's added tests — confirm the
  live number) and keep it green plus the new C.1 tests.
- **Reuse the existing CSV plumbing** — `decode_bytes`, `normalize_name`, the
  `csv` module idiom from `parse_csv`, and the `replace_shoot_memberships` core.
  Don't reimplement decoding/normalization.
- **`frontend/` conventions:** Vite + React 18, JSX, function components + hooks,
  `react-router-dom`, one `styles.css`. **No JS test runner** — frontend sections
  are verified by the manual checks below (same as the capture app), backend
  sections by pytest. Every `fetch` checks `res.ok`; never let an error body land
  in state (the Phase 5 lesson the whole frontend follows).

---

## Build plan (dependency order; each section ends green + committed)

### Section 1 — Backend: roster `inspect` endpoint
`POST /api/players/roster/{job_id}/inspect`. `decode_bytes` → read with the `csv`
module → return `{columns, sample_rows (first ~5), total_rows}`. With
`has_header` the columns are the header cells; the client decides header-ness, so
inspect can return both the raw first row and a positional labeling — keep it
simple: return the first row as `columns` and the next ~5 as `sample_rows`, plus
`total_rows`; the client toggles "first row is a header." 404 if job missing.

**Tests (`test_players.py`):** a headered CSV returns its header names + sample
rows + correct `total_rows`; a quoted-comma field parses as one column (proves we
use the `csv` module, not `split(",")`); unknown job → 404.

**Check:** `pytest -v` green; `curl -F file=@sample.csv …/inspect` returns the
columns.

Commit: `phase C.1 section 1: roster inspect endpoint (+ tests)`

### Section 2 — Backend: mapped parse + strict validation core (pure functions)
In `services/players.py` add (alongside, not replacing, the existing helpers):
- `parse_mapped_roster(text, mapping) -> (canonical_rows, row_errors)` — header-
  or position-based read; assembles `(name, team)` per the mapping; records
  `missing_name`/`missing_team` with 1-based file line numbers (header included).
- `validate_mapping(mapping) -> mapping_errors` — the pre-flight completeness check.
- A small assembler for the report body (`summary`, `preview`).

Pure functions, no I/O, driven directly by tests (testable-core pattern).

**Tests:** full-name mode and split mode both assemble correctly; **last-name-only
is valid**, both-empty is `missing_name`; empty team is `missing_team`; row
numbers match file lines (header counts as row 1); a `Coach-` name still validates
and is flagged a coach; unmapped team / unmapped name(s) → `mapping_errors`; PII
columns present but unmapped are ignored; quoted commas handled.

Commit: `phase C.1 section 2: mapped parse + strict validation core (+ tests)`

### Section 3 — Backend: `mapped` commit endpoint (dry-run, write, refs guard)
`POST /api/players/roster/{job_id}/mapped` wiring Section 2 into HTTP: parse the
`mapping` Form JSON (400 on bad JSON); 404 if job missing; run validation;
`dry_run=true` → return the report (no write); else if invalid → 400 with the
report; else if `ReferenceFace.captured_job_id == job_id` count > 0 and
`!confirm_replace` → 409 `references_exist`; else build rows → call the **unchanged**
`replace_shoot_memberships` → commit → return summary.

**Tests (`test_players.py`, `client` fixture):** dry-run returns `ok:true`/preview
and writes nothing (membership count unchanged); dry-run with bad rows returns the
named-row report; commit happy path writes memberships + flags the coach (then
`GET /api/players/roster/{job_id}` reflects it); commit with bad rows → 400 report;
commit re-upload **replaces** (no duplication); refs-exist → 409 unless
`confirm_replace=true` (seed a `ReferenceFace` with `captured_job_id=job_id` via
the `SessionLocal` the fixture exposes); the positional endpoint still works
unchanged (regression).

Commit: `phase C.1 section 3: mapped commit endpoint + refs-replace guard (+ tests)`

### Section 4 — Frontend: entry point + inspect → columns (UI shell)
New `frontend/src/components/PlayerRosterModal.jsx`, opened from a **new** button
on `JobDetail` distinct from the existing Phase 6 "Upload roster" button
(Section 7 settles labels). The modal: pick a file → POST `/inspect` → show the
detected columns, a "first row is a header" toggle, and a small raw preview.
Loading / error / empty states; `res.ok`-checked fetches. **JobDetail is the home**
(not the creation wizard) because attaching a roster is deferred (Decision intro).

**Manual check:** on a job, open the modal, pick a CSV, see its real columns and a
preview; toggling the header switch relabels columns; a network error shows a
clear message, not a stuck spinner.

Commit: `phase C.1 section 4: player-roster modal + CSV inspect`

### Section 5 — Frontend: the mapping UI
Dropdowns bound to the inspected columns: a **name-mode** toggle (full ↔ split)
that shows one name column or first/last columns accordingly, and a **team**
column dropdown. Live-preview the assembled `(name, team)` for the inspected
sample rows entirely client-side (no server call) so mapping feels immediate.
Mapping state held as the serializable object above.

**Manual check:** switching full↔split swaps the controls; the sample preview
updates live as columns are picked; an unmapped required field disables the
"Validate" action with a hint.

Commit: `phase C.1 section 5: column-mapping UI (full/split name + team)`

### Section 6 — Frontend: validate/preview (strict errors, named rows)
A "Validate" action POSTs `/mapped` with `dry_run=true`. On `ok` show the green
summary ("44 players across 3 teams · 3 coaches") + preview and enable "Upload
roster." On failure render `mapping_errors` and each `row_errors` group verbatim
("3 rows missing a team: rows 12, 34, 51") and **keep upload disabled**. Surface
`references_warning` here as a pre-warning when present.

**Manual check:** a clean CSV validates green; a CSV with a blank team and a blank
name shows both grouped errors with the right Excel row numbers and blocks upload;
fixing the CSV and re-validating clears them.

Commit: `phase C.1 section 6: dry-run validation + named-row error report`

### Section 7 — Frontend: commit + replace guard + coexistence labels
"Upload roster" POSTs `/mapped` (no dry-run). On success show the summary and
refresh. On **409 `references_exist`**, show a confirm ("N players already have
photos captured for this shoot — replace the roster anyway?") and retry with
`confirm_replace=true`. **Labels (Decision 4):** the new button reads e.g.
**"Player roster"** (tooltip: "Roster for reference-photo capture / matching");
relabel the existing Phase 6 button to **"Roster cross-check"** (text/tooltip
only — no behavior change) so the two are unmistakable.

**Manual check:** committing a validated roster writes it (verify via
`GET /api/players/roster/{job_id}`); re-uploading replaces (no doubling); with a
seeded captured reference for the shoot the confirm appears and only proceeds on
confirm; the two roster buttons are clearly different things.

Commit: `phase C.1 section 7: commit + replace-guard confirm + coexistence labels`

### Section 8 — README + sample CSVs + acceptance pass
Document the desktop flow (open Player roster → pick CSV → map columns → validate
→ upload; re-upload replaces) and the A.1-vs-Phase-6 distinction. Add sample CSVs
covering the matrix: a **headered full-name** CSV, a **first/last** CSV, one with
**extra PII columns** (to show they're ignored), and one with a **missing-team
row** (to demonstrate the block). Run the full acceptance list.

Commit: `phase C.1 section 8: README + sample rosters`

---

## Conventions (match existing code)

- Backend: testable-core + thin HTTP wrapper; reuse `decode_bytes` /
  `normalize_name` / the `csv` idiom / `replace_shoot_memberships`; plain-dict
  responses; per-file engine on `tmp_path` in tests, `db` + `client` fixtures, no
  `conftest`; a realistic sample CSV constant. Additive only.
- Frontend: `res.ok`-checked `fetch`; read error bodies via
  `body?.detail?.message || body?.detail?.error`; never store an error object;
  same dark/utility styles as the rest of the app.
- Single-line commit per section (`phase C.1 section N: …`). `pytest -v` green
  after every backend section.

## Acceptance

1. From a job in the desktop app, an operator opens the **Player roster** flow
   (separate from the Phase 6 cross-check), picks a CSV, and sees its real columns.
2. Mapping supports **full-name** and **first-name + last-name** modes plus a team
   column; the sample preview updates live.
3. Validation is **strict**: an incomplete mapping or any row missing name/team
   **blocks the whole upload** and the report **names the offending rows + reason**
   with Excel-matching row numbers. PII columns are ignored.
4. A **last-name-only** row is valid; a row missing name **or** team is not.
5. Committing a valid roster writes `PlayerMembership` rows for the job via the
   unchanged A.1 core, with **coaches flagged** from the `Coach-` prefix;
   `GET /api/players/roster/{job_id}` reflects it.
6. **Re-upload replaces** the job's roster (no duplication, Players reused); when
   reference photos already exist **for this shoot**, replace is gated behind a
   **confirm** (409 → confirm → retry).
7. The positional `POST /api/players/roster/{job_id}` and the Phase 6
   `RosterEntry` system are **byte-for-byte unchanged**; `pytest -v` fully green
   incl. new tests; **no migration** added.
8. The two roster uploads are **clearly labeled** so an operator can't confuse them.

## Out of scope (defer — do NOT build in C.1)

- **Saved mapping templates** (Decision 2) — design-for-later; the mapping object
  is serializable so it drops in as a later phase.
- **Jersey number** (Decision 3) — not mapped, not stored.
- **Writing the Phase 6 `RosterEntry` cross-check roster** from this flow, or
  retiring the wizard's positional step (Decision 4).
- **Any schema change / migration** — none needed.
- **Roster upload inside the creation wizard** — attaching is deferred; JobDetail
  is the home. A wizard hook is a possible later convenience.
- **Fuzzy column auto-detection / "smart" guessing of the mapping**, parent-contact
  ingestion, multi-file/merge uploads, and editing individual roster rows in the UI.
