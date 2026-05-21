"""FastAPI entrypoint for the player-sort backend.

Run on a single machine:
    uvicorn app.main:app --reload --port 8020

Reachable from the LAN (Phase 4.4):
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8020
"""
import warnings

# insightface's transform.py uses np.linalg.lstsq without an explicit rcond;
# numpy >=1.14 prints a FutureWarning per call. The fix is in their code, not
# ours — silence the noise here so logs stay readable.
warnings.filterwarnings(
    "ignore",
    message=r".*`rcond` parameter will change.*",
    category=FutureWarning,
)

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    browse, sessions, clusters, cluster_move, images, jobs, players,
    references, roster, settings,
)
from app.db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # TODO: warm up InsightFace model here so the first request isn't slow.
    # from app.services.face_detector import get_detector
    # get_detector()
    yield


app = FastAPI(title="Player Sort", lifespan=lifespan)

# Allow the Vite dev server from localhost or any RFC1918 private LAN IP on
# port 8021. Anything else (public internet, foreign Wi-Fi) is rejected.
# This is a LAN tool, not a hosted service — don't open it to the world.
_LAN_ORIGIN_REGEX = (
    r"^http://("
    r"localhost"
    r"|127\.0\.0\.1"
    r"|10(\.\d{1,3}){3}"
    r"|192\.168(\.\d{1,3}){2}"
    r"|172\.(1[6-9]|2\d|3[0-1])(\.\d{1,3}){2}"
    r")(:\d+)?$"
)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=_LAN_ORIGIN_REGEX,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(jobs.router, prefix="/api/jobs", tags=["jobs"])
app.include_router(roster.router, prefix="/api/jobs", tags=["roster"])
app.include_router(players.router, prefix="/api/players", tags=["players"])
app.include_router(references.router, prefix="/api/players", tags=["references"])
app.include_router(sessions.router, prefix="/api/sessions", tags=["sessions"])
app.include_router(clusters.router, prefix="/api/sessions", tags=["clusters"])
app.include_router(cluster_move.router, prefix="/api/clusters", tags=["cluster-move"])
app.include_router(images.router, prefix="/api/images", tags=["images"])
app.include_router(settings.router, prefix="/api/settings", tags=["settings"])
app.include_router(browse.router, prefix="/api", tags=["browse"])


@app.get("/api/health")
def health():
    return {"status": "ok"}
