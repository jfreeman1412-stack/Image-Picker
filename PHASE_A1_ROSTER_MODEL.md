# Phase A.1 — Hand-off (Roster model + CSV upload)

Read this in full before starting. This is the **first step** of a larger
multi-phase effort: a mobile/tablet **reference-photo system**. The end state
(later phases, NOT this one) is — a volunteer at check-in taps a player's name
on a tablet, takes a reference photo, the photo gets a face embedding stored
against that player, and the pipeline auto-matches shoot faces against those
references.

**Phase A.1 builds only the foundation that everything else hangs off of:** a
real `Player` record (a unique person) and a per-shoot membership record, plus
the ability to load them from the league roster CSV. There is **no** face
recognition, **no** reference photos, and **no** mobile app in this phase. Do
not build any of that here — see *Out of scope* at the bottom and respect it.

Run `pytest -v` from `backend/` after each section. Confirm the baseline first:
**330 tests as of Phase 11** must stay green, alongside the new ones this phase
adds (~22).

---

## ⚠️ The naming distinction — read this first

There is **already** a model called `RosterEntry` in
[`db_models.py`](backend/app/models/db_models.py). **It is not what this phase
is about, and you must not touch it.** The two things are easy to confuse:

| | Existing `RosterEntry` (Phase 6) | New `PlayerMembership` (this phase) |
|---|---|---|
| Purpose | Roster **cross-check** — flag clusters whose name doesn't match their team | Identity foundation for the **reference-photo** system |
| Shape | Denormalized CSV rows; raw strings only | Normalized: a `Player` (person) + per-shoot membership |
| Person identity? | No — just `raw_name`/`team_name` text | Yes — `Player` is the unique person |
| Scope | Per-job | `Player` is global; `PlayerMembership` is per-shoot |
| Used by | `roster.py` (svc + api), `roster_check.py`, `naming_errors.py`, `guest_clusters.py`, ~40 tests | Brand-new code only |

**Decision (confirmed with the user):** leave the existing `RosterEntry`
completely untouched. The new membership model is named **`PlayerMembership`**.
Both will coexist permanently — that's intentional. They read the **same CSV
format** but populate different tables for different purposes. A future phase
*may* consolidate them, but A.1 does not.

You will **reuse** the existing CSV plumbing from
[`services/roster.py`](backend/app/services/roster.py) — `normalize_name`,
`parse_csv`, `decode_bytes`, `CsvParseError` — rather than reimplementing it.
That's the one place the two systems intentionally share code.

---

## The data model (what you're building)

### `Player` — the unique person

One row per real person, deduplicated across **all** shoots/leagues/seasons.

- **Identity in A.1 is the normalized name.** There is no face data yet, so the
  only identity signal we have is the name the photographer uses (the exact
  string they burn into the EXIF Copyright tag — see `services/roster.py`
  docstring). `norm_name` is the dedup key and is **globally unique**.
- The same kid appearing in three shoots is **one** `Player` with **three**
  `PlayerMembership` rows.

> **Decision (confirmed): global dedup by normalized name.** A `Player` is keyed
> by `norm_name` across all shoots/leagues/seasons. This matches the long-term
> goal of **cross-shoot reference reuse** — a reference photo taken once should
> follow the person to every future shoot. The known limitation is accepted: two
> *different* real kids who share a name (e.g. two "Jack-Smith"s on different
> teams) collapse into one `Player`. False merges are rare, and a later phase
> resolves them with face embeddings — see the split note next.
>
> **Design forward — keep `Player` split-friendly.** A **"Split Player"**
> (un-merge) feature is planned for a later phase. The data model must **not**
> assume a merge is permanent. Concretely: any data future phases attach to a
> `Player` (reference photos, embeddings, etc.) must carry enough **provenance**
> — e.g. which shoot/membership it was captured in — that it can be
> re-partitioned to the correct person when a `Player` is later split in two.
> Don't design future Player data as opaque, un-splittable columns on the
> `Player` row. **A.1 adds no such data, so there is nothing to build now** —
> this is a constraint to honor when those later phases arrive.

### `PlayerMembership` — the per-shoot membership

One row per `(player, shoot, team)`. This is the "RosterEntry" concept from the
original spec, renamed to avoid the collision above.

- A **shoot = a `Job`** (the existing top-level grouping; one Job → many
  Sessions, one Session per team). The roster CSV is per-shoot and lists every
  player across every team in that shoot — exactly the existing per-job CSV
  shape.
- `team_name`/`norm_team` mirror the Phase 6 convention so `norm_team` equals
  `normalize_name(Session.name)`. We store the team as a **string**, not a FK to
  `Session` — rosters can be uploaded before sessions exist, and this matches
  how the existing `RosterEntry` already works. (A nullable `session_id` link is
  a possible future enhancement, **out of scope** here.)
- `is_coach` is derived from the raw name's `Coach-` prefix at parse time (see
  CSV rules below). It lives on the membership because role is contextual to a
  shoot/team.

---

## Context you need

- Migration: [`db.py`](backend/app/db.py) `init_db()` calls
  `Base.metadata.create_all(bind=engine)`, which **creates new tables if they're
  missing and is non-destructive to existing data**. Because A.1 adds only
  **new tables** (no new columns on existing tables), `create_all` is the entire
  migration story — existing `player_sort.db` files keep working untouched.
  **Do NOT add anything to the `_PHASE2_COLUMNS` dict** — that mechanism is only
  for `ALTER TABLE ADD COLUMN` on *existing* tables, which this phase doesn't do.
- CSV plumbing to reuse, all in
  [`services/roster.py`](backend/app/services/roster.py):
  - `normalize_name(s)` — lowercase, NFKD-fold, strip non-alphanumerics. Same
    function the whole codebase uses; use it for both names and teams.
  - `parse_csv(text) -> (rows, skips)` — headerless two-column parser; trims
    cells, skips blank/malformed rows with 1-based line numbers. Already handles
    the exact format A.1 needs.
  - `decode_bytes(data) -> str` — utf-8-sig / utf-8 / cp1252 best-effort decode
    for uploaded files.
  - `CsvParseError` — carries a 1-based line number; raise/translate to HTTP 400.
- Endpoint conventions, see [`roster.py`](backend/app/api/roster.py) and
  [`jobs.py`](backend/app/api/jobs.py):
  - One `router = APIRouter()` per file; mounted with a prefix in
    [`main.py`](backend/app/main.py).
  - `db: DbSession = Depends(get_db)`; 404 via `HTTPException(404, "...")`.
  - **Testable-core pattern:** the real logic lives in a plain function (e.g.
    `load_roster_from_text`) that tests call directly; the HTTP route is a thin
    wrapper that only handles the multipart `UploadFile`. Follow this.
  - Response bodies are plain dicts; list endpoints return `{"items": [...]}`
    or `{"entries": [...]}`-style envelopes.
- Test conventions, see [`test_roster.py`](backend/tests/test_roster.py):
  - **No `conftest.py`.** Each test file builds its own engine on `tmp_path`:
    `create_engine(f"sqlite:///{tmp_path / 'x.db'}", connect_args=...)`,
    `Base.metadata.create_all(bind=engine)`, `sessionmaker(...)`.
  - A `db` fixture yields a session; a `client` fixture sets
    `app.dependency_overrides[get_db]` and uses `TestClient(app)`, seeding a Job
    + Sessions into the same DB the client hits.
  - Use a realistic sample CSV constant (see the `SAMPLE_CSV` shape below).

**Don't break:** the existing `RosterEntry` model and everything that reads it
(Phase 6 roster cross-check, Phase 10 naming-errors, Phase 11 guest-clusters),
the existing `POST/GET/DELETE /api/jobs/{job_id}/roster` endpoints, and all 330
existing tests. This phase is purely **additive** — new tables, new service,
new router, new test file, plus one line in `main.py` to register the router.

---

## Section 1 — Models

In [`db_models.py`](backend/app/models/db_models.py) add two models and one
relationship. Add `UniqueConstraint` to the existing `sqlalchemy` import line.

```python
class Player(Base):
    """A unique person across all shoots/leagues/seasons. Identity in A.1 is
    the normalized name (no face data yet); `norm_name` is the dedup key and is
    globally unique. NOT the same as the Phase 6 `RosterEntry` — see
    PHASE_A1_ROSTER_MODEL.md. Future phases hang a reference photo + face
    embedding off this row.
    """
    __tablename__ = "players"

    id = Column(Integer, primary_key=True)
    norm_name = Column(String, nullable=False, unique=True, index=True)  # dedup key
    display_name = Column(String, nullable=False)   # first-seen raw form, e.g. "Eleanor-Pederson"
    created_at = Column(DateTime, default=datetime.utcnow)
    # Later phases add reference_image_path / embedding here. OUT OF SCOPE for A.1.

    memberships = relationship(
        "PlayerMembership", back_populates="player",
        cascade="all, delete-orphan",
    )


class PlayerMembership(Base):
    """One row per (player, shoot, team). `job_id` is the shoot. `is_coach` is
    derived from the raw name's 'Coach-' prefix at parse time. `norm_team`
    matches normalize_name(Session.name)."""
    __tablename__ = "player_memberships"
    __table_args__ = (
        Index("ix_player_memberships_job", "job_id"),
        Index("ix_player_memberships_player", "player_id"),
        UniqueConstraint("job_id", "player_id", "norm_team",
                         name="uq_membership_job_player_team"),
    )

    id = Column(Integer, primary_key=True)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=False)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False)
    team_name = Column(String, nullable=False)   # raw, as in CSV
    norm_team = Column(String, nullable=False)   # matches normalize_name(Session.name)
    is_coach = Column(Integer, default=0)        # 0 | 1, from "Coach-" name prefix
    created_at = Column(DateTime, default=datetime.utcnow)

    player = relationship("Player", back_populates="memberships")
    job = relationship("Job", back_populates="player_memberships")
```

On the existing `Job` model, add (next to the existing `roster_entries`
relationship):

```python
    player_memberships = relationship(
        "PlayerMembership", back_populates="job", cascade="all, delete-orphan"
    )
```

**Cascade semantics (important):** deleting a `Job` cascades to its
`PlayerMembership` rows but **not** to `Player` rows — Players are global and
shared across shoots. A Player left with zero memberships becomes orphaned;
that's acceptable in A.1 (the Player is the identity anchor a reference photo
will later attach to). Orphan cleanup is **out of scope.**

No `db.py` change is required — `create_all` builds both new tables at startup.

### Tests for Section 1 (`backend/tests/test_players.py`)

- Both tables are created by `Base.metadata.create_all` (the fixture proves it
  by inserting a `Player` + `PlayerMembership`).
- `Player.norm_name` unique constraint: inserting two Players with the same
  `norm_name` raises (IntegrityError).
- `Job` → `player_memberships` cascade: deleting a Job removes its memberships
  but leaves the `Player` rows intact (covered fully in Section 2's tests once
  the loader exists — a stub here is fine).

Commit: `phase A.1 section 1: Player + PlayerMembership models`

---

## Section 2 — CSV parse + load service

New file `backend/app/services/players.py`. **Reuse** `normalize_name`,
`parse_csv`, `decode_bytes`, `CsvParseError` from `services/roster.py` — import
them, do not reimplement.

### CSV format (same as the Phase 6 roster CSV, with one added rule)

Two columns, **no header**: `Player-Name,Team-Name`. Coaches are identified by a
name that starts with `Coach-`. Example (the real Princeton sample shape):

```
Eleanor-Pederson,10U-Black-Softball
June-Wampach,10U-Black-Softball
Brooks-Cruz-Carter,11UA-Baseball
Lincoln-St-Marie,9UAA-Baseball
carter-Johanson,Majors-2-Baseball
Coach-10UBlack-SB,10U-Black-Softball
```

### Coach detection

```python
COACH_PREFIX = "coach-"

def is_coach_name(raw_name: str) -> bool:
    """A roster name is a coach iff it starts with 'Coach-' (case-insensitive),
    hyphen included. 'Coachman-Lee' is NOT a coach (no hyphen after 'Coach')."""
    return raw_name.strip().lower().startswith(COACH_PREFIX)
```

The `Coach-` prefix stays part of the name/identity (we do **not** strip it) —
consistent with the codebase's "the name string is exactly what the photographer
uses" principle. A coach therefore normalizes to a distinct `norm_name` and is a
distinct `Player` from any player.

### Player upsert

```python
def upsert_player(db, raw_name) -> tuple[Player | None, bool]:
    """Find-or-create a Player by normalized name. Returns (player, created).
    Returns (None, False) for a row whose normalized name is empty (junk) — the
    caller skips it. Uses db.flush() so the new id is available immediately."""
    norm = normalize_name(raw_name)
    if not norm:
        return None, False
    player = db.query(Player).filter_by(norm_name=norm).one_or_none()
    if player is not None:
        return player, False
    player = Player(norm_name=norm, display_name=raw_name.strip())
    db.add(player)
    db.flush()
    return player, True
```

`display_name` keeps the **first-seen** raw form (so `carter-Johanson` and a
later `Carter-Johanson` resolve to the same Player, displaying the first form).

### Replace a shoot's memberships

```python
def replace_shoot_memberships(db, job_id, rows) -> dict:
    """Wipe this job's memberships, upsert Players, insert fresh memberships.
    Caller commits. Idempotent: re-running with the same rows yields the same
    membership count (not doubled) and does not duplicate Players. Does NOT
    touch other jobs' memberships or any Player's other memberships."""
    db.query(PlayerMembership).filter_by(job_id=job_id).delete(synchronize_session=False)
    players_created = players_existing = memberships_loaded = 0
    rows_skipped_blank_name = coaches = 0
    teams: set[str] = set()
    for raw_name, team_name in rows:
        player, created = upsert_player(db, raw_name)
        if player is None:
            rows_skipped_blank_name += 1
            continue
        players_created += int(created)
        players_existing += int(not created)
        coach = is_coach_name(raw_name)
        db.add(PlayerMembership(
            player_id=player.id, job_id=job_id,
            team_name=team_name, norm_team=normalize_name(team_name),
            is_coach=1 if coach else 0,
        ))
        memberships_loaded += 1
        coaches += int(coach)
        teams.add(normalize_name(team_name))
    db.flush()
    return {
        "players_created": players_created,
        "players_existing": players_existing,
        "memberships_loaded": memberships_loaded,
        "coaches": coaches,
        "distinct_teams": len(teams),
        "rows_skipped_blank_name": rows_skipped_blank_name,
    }
```

### Endpoint-helper (testable core, mirrors `load_roster_from_text`)

```python
def load_shoot_roster_from_text(db, job_id, text) -> dict:
    """Parse CSV text, atomically replace this shoot's memberships, commit,
    return the summary dict. Tests drive this directly. 404 if job missing."""
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    rows, skips = parse_csv(text)
    summary = replace_shoot_memberships(db, job_id, rows)
    db.commit()
    summary["entries_skipped"] = len(skips)  # malformed/blank CSV rows
    return summary
```

(Allowing a player on two different teams in the same shoot is **valid** — it
produces two memberships. The `uq_membership_job_player_team` constraint only
forbids the exact same player on the exact same team twice in one shoot.)

### Tests for Section 2

Coach detection:
- `Coach-10UBlack-SB` → coach; lowercase `coach-x` → coach; `Eleanor-Pederson`
  → not; `Coachman-Lee` → not (no `Coach-` prefix).

Player identity / upsert:
- New name → new Player; same normalized name again → reuses, `created=False`.
- `carter-Johanson` then `Carter-Johanson` → one Player, `display_name` is the
  first-seen `carter-Johanson`.
- A row whose name normalizes to empty (e.g. `"---"`) → skipped, no Player.

Membership loading:
- Loads one membership per valid row; coach rows get `is_coach=1`, players `0`.
- **Cross-shoot identity:** load shoot (job) A then shoot B sharing a name →
  ONE Player, TWO memberships; `players_existing` counts the reuse in B.
- **Re-upload replaces, not appends:** loading the same CSV twice for a job
  yields the same membership count and does not duplicate Players.
- Re-uploading job A's roster does **not** delete job B's memberships or any
  Player.
- A player listed on two teams in one shoot → two memberships.
- Malformed/blank CSV rows are skipped and counted in `entries_skipped`.
- Job cascade: `db.delete(job)` removes that job's memberships, leaves Players.
- Unknown job id → `HTTPException(404)`.

Commit: `phase A.1 section 2: roster CSV parse + Player/membership load service`

---

## Section 3 — API endpoints

New file `backend/app/api/players.py` with `router = APIRouter()`. Register it in
[`main.py`](backend/app/main.py): add `players` to the `from app.api import (...)`
line and `app.include_router(players.router, prefix="/api/players", tags=["players"])`.
This is the only edit to an existing file in this phase.

> Declare the static-segment routes (`/roster/...`) **before** the dynamic
> `/{player_id}` route so FastAPI matches them correctly.

Endpoints:

```
POST   /api/players/roster/{job_id}   upload (multipart CSV) — replaces this shoot's memberships
GET    /api/players/roster/{job_id}   list this shoot's memberships (the check-in roster)
DELETE /api/players/roster/{job_id}   clear this shoot's memberships
GET    /api/players                   list/query unique players (?q=, ?limit=, ?offset=)
GET    /api/players/{player_id}       one player + all their memberships across shoots
```

- `POST /api/players/roster/{job_id}`: read the `UploadFile`, `decode_bytes`,
  call `load_shoot_roster_from_text`. Translate `CsvParseError` →
  `HTTPException(400, detail={"error": "csv_parse", "line": ..., "message": ...})`,
  exactly like `roster.py` does. Returns the Section-2 summary dict.
- `GET /api/players/roster/{job_id}`: 404 if job missing. Returns
  `{"memberships_loaded": N, "items": [{"player_id", "name" (display_name),
  "team": team_name, "is_coach": bool}, ...], "distinct_teams": K}` ordered by
  membership id.
- `DELETE /api/players/roster/{job_id}`: 404 if job missing; delete this job's
  memberships, return `{"deleted": N}`. (Leaves Players — same orphan rule.)
- `GET /api/players`: list all Players ordered by `display_name`. `?q=`
  filters by `normalize_name(q)` substring against `norm_name` (so "eleanor"
  matches "Eleanor-Pederson"). `?limit` (default 100) / `?offset` (default 0)
  for paging. Returns `{"items": [{"id", "name": display_name,
  "membership_count": M}, ...], "total": <count before paging>}`.
- `GET /api/players/{player_id}`: 404 if missing. Returns
  `{"id", "name": display_name, "memberships": [{"job_id", "team": team_name,
  "is_coach": bool}, ...]}`.

### Tests for Section 3

Use a `client` fixture like `test_roster.py`'s — `dependency_overrides[get_db]`,
`TestClient(app)`, seed a Job (and a couple of Sessions, for parity).

- POST multipart upload → 200 + summary (`memberships_loaded`, `coaches`,
  `players_created`); then `GET /api/players/roster/{job_id}` reflects it with
  the coach flagged; then `DELETE` returns the count and a follow-up GET shows 0.
- POST to an unknown job id → 404.
- POST malformed-but-parseable CSV (blank/short rows) → 200 with
  `entries_skipped` > 0.
- `GET /api/players` lists all unique players; `?q=eleanor` filters to the
  match; `?limit/offset` pages.
- `GET /api/players/{id}` returns the player with memberships across two
  uploaded shoots (proves cross-shoot identity end-to-end over HTTP); unknown id
  → 404.

Commit: `phase A.1 section 3: players API (shoot roster upload + player query)`

---

## Conventions (match the existing code)

- Reuse `normalize_name` / `parse_csv` / `decode_bytes` / `CsvParseError` from
  `services/roster.py`. Don't duplicate normalization or parsing logic.
- Keep the testable-core-plus-thin-HTTP-wrapper split (the
  `load_shoot_roster_from_text` pattern).
- Plain-dict responses; `{"items": [...]}` envelopes for lists.
- New test file builds its own engine on `tmp_path` (no shared `conftest.py`);
  realistic sample CSV including a `Coach-` row.
- Single-line commit messages per section (`phase A.1 section N: ...`).
- `pytest -v` green after every section: 330 existing + new (~22 → ~352).

## Acceptance

1. `players` and `player_memberships` tables exist after `init_db()`; existing
   `player_sort.db` files open with **no** migration error and **no** data loss
   (only new tables added).
2. The existing `RosterEntry` model and its endpoints/tests are byte-for-byte
   unchanged; all 330 prior tests still pass.
3. Uploading the sample CSV for a shoot creates one `Player` per unique
   normalized name, one `PlayerMembership` per valid row, and flags the
   `Coach-` row as a coach.
4. Uploading rosters for two shoots that share a name produces ONE Player with
   TWO memberships, visible via `GET /api/players/{id}`.
5. Re-uploading a shoot's roster replaces its memberships (no duplication) and
   reuses existing Players; deleting a Job removes its memberships but not the
   Players.
6. `GET /api/players?q=` filters by normalized name; `pytest -v` is fully green.

## Out of scope (defer — do NOT build in A.1)

- **Face recognition, reference photos, embeddings.** No image columns on
  `Player`, no embedding storage, no matching. (Those are later phases — A.1 is
  just the roster spine they attach to.)
- **The mobile/tablet app** and any check-in UI.
- **Auto-matching shoot faces against references.**
- **Linking `PlayerMembership` to `Session`** via FK (we store team as a
  string, like the existing roster). A nullable `session_id` is a future option.
- **Rich upload warnings** (unmatched-CSV-team / session-missing-from-roster /
  duplicate-name) like Phase 6's `_warnings_for`. A.1's summary stays lean
  (`entries_skipped` is enough); session-matching warnings can come later.
- **Player merge/split UI and the "Split Player" (un-merge) feature** — planned
  for a later phase, not A.1. Note the forward constraint in the identity
  decision above: future `Player` data must stay split-friendly (provenance-
  tagged) so a merge can be undone. Also out of scope: **orphaned-Player
  cleanup.**
- **Touching or consolidating the existing `RosterEntry`.** Both models coexist
  on purpose (see the naming table at the top).
- **Frontend.** A.1 is backend + tests only; no React changes.
