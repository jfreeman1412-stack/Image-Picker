"""In-memory active-trigger tracking for the pipeline endpoints.

Fix 2 of 3 (2026-09-14) for the pipeline-concurrency-wedge. Fix 1
(face_pipeline.py's _PIPELINE_LOCK) made concurrent triggers SAFE by
serializing them; Fix 2 makes redundant triggers REJECTED so a second
click on Run All doesn't quietly queue an entire duplicate wave of work
behind the lock. Fix 3 (watchdog) reaps stale status='running' rows —
independent of Fix 2 by design.

Both `POST /api/jobs/{id}/run-all` and `POST /api/sessions/{id}/run` need
to atomically check each other's state so the mixed race (individual
/run for session S ∈ job J firing concurrently with /run-all for J)
resolves cleanly. Rather than cross-import each module's set + lock
(circular import + lock-order minefield), both endpoints import ACTIVE_*
sets and the single TRIGGER_LOCK from here. One lock, two sets, atomic
across the pair. In-memory + process-local + self-cleaning via `finally`
in each endpoint's background closure — no DB dependency and no risk of
a stale status='running' locking a job out (that's the watchdog's job).

Membership contract:
  - ACTIVE_RUN_ALLS holds job_ids currently mid-run-all.
  - ACTIVE_SESSION_RUNS holds session_ids currently mid-individual-run.
  - run-all takes a slot in ACTIVE_RUN_ALLS AND additionally reserves
    its session_ids in ACTIVE_SESSION_RUNS so an incoming /run for one
    of those sessions sees the reservation and 409s. The reservation is
    released alongside the job_id in the run-all `finally`.
  - /run takes a slot in ACTIVE_SESSION_RUNS only. It also refuses if
    the session's parent job is in ACTIVE_RUN_ALLS (defense against
    stale reservation state — the set entries should already reflect
    this, but the parent-job check is cheap and unambiguous).

Never leak an id: the `finally` in the background closure is
load-bearing. A leaked id would permanently lock out that job/session
until backend restart, which is a strictly worse failure mode than the
wasteful double-run this fix prevents. Tests explicitly exercise the
exception path to prove `finally` releases on both success and error.
"""
import threading

TRIGGER_LOCK = threading.Lock()

# Populated by app.api.jobs.run_all; released by that endpoint's
# background closure's `finally`.
ACTIVE_RUN_ALLS: set[int] = set()

# Populated by app.api.sessions.run AND by app.api.jobs.run_all (the
# latter reserves every non-archived session_id upfront so /run coordinates
# with it). Released by the respective endpoint's `finally`.
ACTIVE_SESSION_RUNS: set[int] = set()
