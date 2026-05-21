# Phase A.1 — DONE

Implemented `PHASE_A1_ROSTER_MODEL.md` in full. Three sections, three commits,
plus this summary. Branch: `phase-A1-overnight`.

## What got built

The identity spine for the future reference-photo system. **Purely additive** —
two new tables, a new service, a new router, a new test file, and a 2-line
registration in `main.py`. The existing Phase 6 `RosterEntry` and everything
that reads it are byte-for-byte unchanged.

### Section 1 — Models (`backend/app/models/db_models.py`)
- **`Player`** — one row per unique person, globally deduped by `norm_name`
  (unique, indexed). `display_name` keeps the first-seen raw form.
- **`PlayerMembership`** — one row per `(player, shoot=job, team)`. Stores
  `team_name`/`norm_team` as strings (no Session FK, like the existing roster),
  plus `is_coach`. Unique constraint `uq_membership_job_player_team` +
  per-job/per-player indexes.
- Added `player_memberships` relationship to `Job` with
  `cascade="all, delete-orphan"`: deleting a Job removes its memberships but
  **not** the global Players. Added `UniqueConstraint` to the sqlalchemy import.
- No `db.py` change — `Base.metadata.create_all` builds both new tables at
  startup (verified: `init_db()` adds `players` + `player_memberships`, leaves
  `roster_entries` and all existing data intact).

### Section 2 — Load service (`backend/app/services/players.py`)
Reuses `normalize_name` / `parse_csv` / `decode_bytes` / `CsvParseError` from
`services/roster.py` (the one intentional shared seam). Functions:
- `is_coach_name(raw)` — true iff name starts with `Coach-` (case-insensitive,
  hyphen required; `Coachman-Lee` is not a coach). Prefix is kept in the name.
- `upsert_player(db, raw)` — find-or-create by `norm_name`; `(None, False)` for
  junk that normalizes to empty.
- `replace_shoot_memberships(db, job_id, rows)` — wipe-and-reload this shoot's
  memberships; idempotent; returns a lean summary dict.
- `load_shoot_roster_from_text(db, job_id, text)` — testable core: parse +
  replace + commit; 404 if job missing.

### Section 3 — API (`backend/app/api/players.py`, registered in `main.py`)
- `POST   /api/players/roster/{job_id}` — multipart CSV upload, replaces shoot
  (CsvParseError → 400 with `{error, line, message}`, like `roster.py`).
- `GET    /api/players/roster/{job_id}` — this shoot's memberships (check-in
  roster), coach flagged.
- `DELETE /api/players/roster/{job_id}` — clear shoot memberships (leaves Players).
- `GET    /api/players` — list/query unique players (`?q=` normalized substring,
  `?limit`/`?offset` paging, `total` before paging, per-page `membership_count`).
- `GET    /api/players/{player_id}` — one player + memberships across all shoots.
- Static `/roster/...` routes declared before dynamic `/{player_id}`.

## Test count

- **Baseline (Phase 11): 330 passed** — confirmed green at startup before any change.
- **New this phase: 22** in `backend/tests/test_players.py` (Section 1: 3,
  Section 2: 11, Section 3: 8 — matches the hand-off's ~22 estimate).
- **Final: 352 passed**, 0 failed. No existing test modified.

## Commits

```
7df679c phase A.1 section 3: players API (shoot roster upload + player query)
6be6da6 phase A.1 section 2: roster CSV parse + Player/membership load service
61fdbf9 phase A.1 section 1: Player + PlayerMembership models
```

## What to verify in the morning

1. **Tests:** `cd backend && pytest -v` → 352 passed (330 prior + 22 new).
2. **Migration safety:** the real `backend/data/player_sort.db` opens with no
   error and gains exactly two tables (`players`, `player_memberships`); no
   existing data touched. (Verified via `init_db()` against the live DB.)
3. **RosterEntry untouched:** `git diff 2e2de2d HEAD -- backend/app/services/roster.py
   backend/app/api/roster.py backend/tests/test_roster.py` is empty.
4. **Acceptance walk-through (optional, by hand or curl):**
   - Upload the Princeton sample CSV to a shoot → 8 Players, 8 memberships, the
     `Coach-` row flagged `is_coach`.
   - Upload a 2nd shoot sharing a name → `GET /api/players/{id}` shows ONE
     player with TWO memberships (cross-shoot identity).
   - Re-upload a shoot → membership count unchanged, no duplicate Players;
     deleting a Job removes its memberships but leaves the Players.
   - `GET /api/players?q=eleanor` filters by normalized name.

## Out of scope (deferred, as instructed — NOT built)

Face recognition / reference photos / embeddings; mobile/tablet check-in app;
auto-matching; `PlayerMembership`→`Session` FK; rich upload warnings; merge/split
UI + orphan cleanup; any frontend. The forward constraint stands: future
`Player` data must stay provenance-tagged so a merge can be undone.
