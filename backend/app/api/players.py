"""Phase A.1 — Player identity + per-shoot roster endpoints.

The reference-photo system's spine. Distinct from the Phase 6 roster
cross-check (`/api/jobs/{job_id}/roster`) — that flags name/team mismatches;
this builds a global `Player` (unique person) + per-shoot `PlayerMembership`.
See PHASE_A1_ROSTER_MODEL.md.

    POST   /api/players/roster/{job_id}                  upload (multipart CSV) — replaces shoot
    GET    /api/players/roster/{job_id}                  list this shoot's memberships
    GET    /api/players/roster/{job_id}/reference-status which players have a photo FOR this shoot
    DELETE /api/players/roster/{job_id}                  clear this shoot's memberships
    GET    /api/players                                  list/query unique players (?q,?limit,?offset)
    GET    /api/players/{player_id}                      one player + memberships across shoots

Static `/roster/...` routes are declared BEFORE the dynamic `/{player_id}`
route so FastAPI matches them correctly.
"""
import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import func
from sqlalchemy.orm import Session as DbSession

from app.db import get_db
from app.models.db_models import Job, Player, PlayerMembership, ReferenceFace
from app.services.players import load_shoot_roster_from_text
from app.services.roster import CsvParseError, decode_bytes, normalize_name

logger = logging.getLogger(__name__)
router = APIRouter()


# ── per-shoot roster (the check-in roster) ───────────────────────────────

@router.post("/roster/{job_id}")
async def upload_shoot_roster(
    job_id: int,
    file: UploadFile = File(...),
    db: DbSession = Depends(get_db),
):
    """Upload a shoot's roster CSV — replaces this shoot's memberships."""
    data = await file.read()
    text = decode_bytes(data)
    try:
        return load_shoot_roster_from_text(db, job_id, text)
    except CsvParseError as exc:
        raise HTTPException(400, detail={
            "error": "csv_parse",
            "line": exc.line,
            "message": str(exc),
        })


@router.get("/roster/{job_id}")
def get_shoot_roster(job_id: int, db: DbSession = Depends(get_db)):
    """List this shoot's memberships (the check-in roster)."""
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    memberships = (
        db.query(PlayerMembership)
        .filter_by(job_id=job_id)
        .order_by(PlayerMembership.id.asc())
        .all()
    )
    return {
        "memberships_loaded": len(memberships),
        "items": [
            {
                "player_id": m.player_id,
                "name": m.player.display_name,
                "team": m.team_name,
                "is_coach": bool(m.is_coach),
            }
            for m in memberships
        ],
        "distinct_teams": len({m.norm_team for m in memberships}),
    }


@router.get("/roster/{job_id}/reference-status")
def shoot_reference_status(job_id: int, db: DbSession = Depends(get_db)):
    """Phase B.2 — which players already have a reference photo captured FOR
    THIS shoot (the check-in ✓ status). **Shoot-scoped:** a reference captured
    for a different shoot, or with no provenance (`captured_job_id IS NULL`,
    e.g. a B.1 upload), does NOT count — each shoot needs its own photo. 404 if
    the job is missing. Counts a player regardless of whether they're still on
    the current roster; the caller only badges players it displays."""
    if db.query(Job).get(job_id) is None:
        raise HTTPException(404, "Job not found")
    rows = (
        db.query(ReferenceFace.player_id)
        .filter(ReferenceFace.captured_job_id == job_id)
        .distinct()
        .all()
    )
    return {"player_ids_with_references": [pid for (pid,) in rows]}


@router.delete("/roster/{job_id}")
def delete_shoot_roster(job_id: int, db: DbSession = Depends(get_db)):
    """Clear this shoot's memberships. Leaves Players (they're global)."""
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    n = db.query(PlayerMembership).filter_by(job_id=job_id).delete()
    db.commit()
    return {"deleted": n}


# ── global player query ──────────────────────────────────────────────────

@router.get("")
def list_players(
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
    db: DbSession = Depends(get_db),
):
    """List unique players ordered by display_name. `?q=` filters by
    normalized-name substring (so "eleanor" matches "Eleanor-Pederson").
    `total` is the match count before paging."""
    query = db.query(Player)
    norm_q = normalize_name(q) if q else ""
    if norm_q:
        query = query.filter(Player.norm_name.contains(norm_q))
    total = query.count()
    players = (
        query.order_by(Player.display_name.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    # One grouped query for this page's membership counts (no N+1).
    ids = [p.id for p in players]
    counts: dict[int, int] = {}
    if ids:
        for pid, cnt in (
            db.query(PlayerMembership.player_id, func.count(PlayerMembership.id))
            .filter(PlayerMembership.player_id.in_(ids))
            .group_by(PlayerMembership.player_id)
            .all()
        ):
            counts[pid] = cnt
    return {
        "items": [
            {"id": p.id, "name": p.display_name, "membership_count": counts.get(p.id, 0)}
            for p in players
        ],
        "total": total,
    }


@router.get("/{player_id}")
def get_player(player_id: int, db: DbSession = Depends(get_db)):
    """One player + all their memberships across shoots (cross-shoot identity)."""
    player = db.query(Player).get(player_id)
    if player is None:
        raise HTTPException(404, "Player not found")
    return {
        "id": player.id,
        "name": player.display_name,
        "memberships": [
            {
                "job_id": m.job_id,
                "team": m.team_name,
                "is_coach": bool(m.is_coach),
            }
            for m in sorted(player.memberships, key=lambda m: m.id)
        ],
    }
