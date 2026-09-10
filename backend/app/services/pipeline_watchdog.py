"""Fix 3 of the 2026-09-10 pipeline-concurrency-wedge response.

A daemon thread that reaps sessions stuck at status='running' past a
sane threshold and demotes them to status='error'. Complement to Fix 1
(the _PIPELINE_LOCK in face_pipeline.py that serializes new work): the
lock prevents future wedges, this watchdog cleans up the ones we
already have (sid=642 is 83 days old) and catches any deadlock the
lock can't prevent (native-code hangs, OOM kills, whatever).

Design principles:
- Truth signal: `Session.status_updated_at`, auto-stamped on any status
  change via the model-level attribute event. Single unambiguous rule
  for "how long has this row been at 'running'."
- Startup pass: on backend boot, ANY running session with
  status_updated_at BEFORE _STARTUP_AT is dead by definition (its
  pipeline thread died with the previous process). Reap immediately.
- Steady-state: every 60s, reap sessions whose status_updated_at is
  older than 20 minutes. Threshold picked from real data (see memory
  pipeline-concurrency-wedge — longest observed classifying-stage
  duration extrapolates to ~3 min; 20 min = ~7x margin).
- Does NOT retry / restart / re-run anything. Reaping is only
  status='error' + progress_stage='error' + log line. The human sees
  the failure and decides what to do. Self-healing hides real bugs.
- Startup-race safety: reaper uses `status_updated_at < _STARTUP_AT`
  for the startup pass, so a /run triggered AFTER boot (stamping
  status_updated_at = now) is untouched. Regular passes use the 20-min
  threshold, so a legit boot-time /run still has ~20 min of grace.
"""
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import or_

from app.db import SessionLocal
from app.models.db_models import Session

logger = logging.getLogger(__name__)


# How often the steady-state reaper wakes up.
_WATCHDOG_INTERVAL_SECONDS = 60

# Any session with status='running' whose status_updated_at is older than
# this is stale. See threshold derivation in memory
# pipeline-concurrency-wedge — chosen well above the longest observed
# legitimate stage duration (~3 min classifying on 421-image sessions).
_WATCHDOG_STAGE_STALE_SECONDS = 20 * 60

# Sleep after start_watchdog() before the startup pass, so any legitimate
# /run triggered exactly at boot has time to reach `status='running'` +
# stamp `status_updated_at` before the reaper looks. Not strictly required
# (the status_updated_at < _STARTUP_AT filter is what makes the pass
# race-safe), but it's a belt-and-suspenders margin.
_WATCHDOG_STARTUP_GRACE_SECONDS = 30


# Captured at start_watchdog() call time; used by the startup pass to
# distinguish "was already running when we booted" from "just started up
# post-boot."
_STARTUP_AT: Optional[datetime] = None
_STARTUP_AT_LOCK = threading.Lock()


def _get_startup_at() -> Optional[datetime]:
    with _STARTUP_AT_LOCK:
        return _STARTUP_AT


def _set_startup_at(when: datetime) -> None:
    global _STARTUP_AT
    with _STARTUP_AT_LOCK:
        _STARTUP_AT = when


def reap_stale(*, startup: bool, now: Optional[datetime] = None,
               session_factory=None) -> list[int]:
    """Reap sessions with status='running' whose status_updated_at is
    older than the appropriate threshold.

    Args:
        startup: If True, threshold is _STARTUP_AT (any pre-boot row is
            stale by definition — the previous process's threads died).
            If False, threshold is now - _WATCHDOG_STAGE_STALE_SECONDS
            (steady-state 20-min staleness).
        now: Injectable clock for tests; defaults to datetime.utcnow().
        session_factory: Injectable DB session factory for tests;
            defaults to the module-level SessionLocal.

    Returns:
        List of reaped session ids. Useful for tests + log follow-up.

    Reaping = set status='error', progress_stage='error', commit. Nothing
    else — no retry, no restart, no _clear_prior_results.
    """
    now = now or datetime.utcnow()
    factory = session_factory or SessionLocal

    if startup:
        threshold = _get_startup_at()
        if threshold is None:
            # start_watchdog() was never called — refuse to reap. Fail
            # closed on the "was this pass legitimately scheduled" question.
            logger.warning(
                "[watchdog] reap_stale(startup=True) called but "
                "_STARTUP_AT is unset; skipping to avoid mass-reaping."
            )
            return []
        reason = "startup"
    else:
        threshold = now - timedelta(seconds=_WATCHDOG_STAGE_STALE_SECONDS)
        reason = "stale"

    reaped: list[int] = []
    with factory() as db:
        # OR IS NULL: pre-migration rows have status_updated_at=NULL. Any
        # such row still marked 'running' is a zombie by definition (its
        # status was set before we started tracking timestamps). Include
        # them so the very first reap on a freshly-migrated DB catches
        # sid=642-style ancient zombies without a backfill step.
        stale = db.query(Session).filter(
            Session.status == "running",
            or_(
                Session.status_updated_at < threshold,
                Session.status_updated_at.is_(None),
            ),
        ).all()

        for s in stale:
            elapsed_desc = "unknown"
            if s.status_updated_at is not None:
                elapsed = (now - s.status_updated_at).total_seconds()
                elapsed_desc = f"{elapsed:.0f}s"
            logger.warning(
                "[watchdog] Reaping stale pipeline: sid=%s job=%s name=%r "
                "stage=%r status_updated_at=%s elapsed=%s reason=%s",
                s.id, s.job_id, s.name, s.progress_stage,
                s.status_updated_at, elapsed_desc, reason,
            )
            s.status = "error"
            s.progress_stage = "error"
            reaped.append(s.id)
        db.commit()

    if reaped:
        logger.warning(
            "[watchdog] reap_stale(startup=%s) reaped %d session(s): %s",
            startup, len(reaped), reaped,
        )
    return reaped


def _watchdog_loop() -> None:
    """The thread body. Grace period → startup pass → forever loop.

    Individual reap_stale calls are wrapped in try/except so a transient
    DB hiccup doesn't kill the whole watchdog — the next tick just
    retries. Failing quietly on repeated errors would be silent-success
    territory (see memory silent-success-equals-silent-failure), so
    every failure logs at WARNING level.
    """
    time.sleep(_WATCHDOG_STARTUP_GRACE_SECONDS)
    try:
        reap_stale(startup=True)
    except Exception:  # noqa: BLE001 — never let a bad pass kill the thread
        logger.exception("[watchdog] startup reap failed; continuing")

    while True:
        time.sleep(_WATCHDOG_INTERVAL_SECONDS)
        try:
            reap_stale(startup=False)
        except Exception:  # noqa: BLE001 — same reasoning as startup pass
            logger.exception("[watchdog] periodic reap failed; continuing")


def start_watchdog() -> threading.Thread:
    """Kick off the watchdog daemon. Called from FastAPI's lifespan
    startup. Returns the thread so callers can inspect / join if
    desired (tests use this)."""
    _set_startup_at(datetime.utcnow())
    thread = threading.Thread(
        target=_watchdog_loop, daemon=True, name="pipeline-watchdog",
    )
    thread.start()
    logger.warning(
        "[watchdog] pipeline watchdog started (grace=%ss, "
        "interval=%ss, stale_threshold=%ss)",
        _WATCHDOG_STARTUP_GRACE_SECONDS,
        _WATCHDOG_INTERVAL_SECONDS,
        _WATCHDOG_STAGE_STALE_SECONDS,
    )
    return thread
