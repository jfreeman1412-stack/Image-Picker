"""Phase A.3 — matching debug endpoint.

    GET /api/matching/face/{face_id}   match a stored shoot face's embedding
                                       against all references

Manual inspection / calibration only — NOT consumed by the pipeline in A.3
(integration is A.4). The real logic lives in services/matching.py; this is a
thin wrapper that resolves face_id → embedding. See PHASE_A3_MATCHING_SERVICE.md.
"""
import logging

import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session as DbSession

from app.db import get_db
from app.models.db_models import Face
from app.services import matching

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/face/{face_id}")
def match_face(face_id: int, db: DbSession = Depends(get_db)):
    """Match a stored shoot face's embedding against all stored references.
    Echoes the active thresholds so calibration is legible from the wire."""
    face = db.query(Face).get(face_id)
    if face is None:
        raise HTTPException(404, "Face not found")
    embedding = np.frombuffer(face.embedding, dtype=np.float32)
    result = matching.match_embedding(db, embedding)
    result["thresholds"] = {
        "high": matching.HIGH_THRESHOLD,
        "low": matching.LOW_THRESHOLD,
        "margin": matching.MIN_MARGIN,
    }
    return result
