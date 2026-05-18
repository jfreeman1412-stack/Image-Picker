"""SQLite + SQLAlchemy session management."""
import logging
from pathlib import Path
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "player_sort.db"

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# Phase 2 schema additions. Auto-applied at startup via ALTER TABLE so existing
# player_sort.db files keep working. If you'd rather start fresh, just delete
# backend/data/player_sort.db.
_PHASE2_COLUMNS = {
    "faces": [
        ("age", "FLOAT"),
        ("yaw", "FLOAT"),
        ("pitch", "FLOAT"),
        ("face_area_ratio", "FLOAT"),
    ],
    "clusters": [
        ("is_likely_coach", "INTEGER DEFAULT 0"),
        ("auto_label", "VARCHAR"),
        ("manual_label", "VARCHAR"),
        ("manual_coach_override", "INTEGER DEFAULT 0"),
    ],
    "image_roles": [
        ("manual_override", "INTEGER DEFAULT 0"),
    ],
    # Phase 4: Session gets a nullable job_id pointing at the new jobs table.
    # Phase 4.2: reviewed flag + timestamp for review-nav muscle memory.
    # Phase 4.5: progress tracking for the running pipeline.
    "sessions": [
        ("job_id", "INTEGER"),
        ("reviewed", "INTEGER DEFAULT 0"),
        ("reviewed_at", "DATETIME"),
        ("progress_stage", "VARCHAR"),
        ("progress_current", "INTEGER DEFAULT 0"),
        ("progress_total", "INTEGER DEFAULT 0"),
    ],
    # Soft-archive flag for jobs that should drop off the default home view.
    # Phase 4.5: async ingest status/progress fields.
    "jobs": [
        ("archived", "INTEGER DEFAULT 0"),
        ("archived_at", "DATETIME"),
        ("ingest_status", "VARCHAR DEFAULT 'done'"),
        ("ingest_progress", "INTEGER DEFAULT 0"),
        ("ingest_total", "INTEGER DEFAULT 0"),
        ("ingest_current_team", "VARCHAR"),
        ("ingest_error", "VARCHAR"),
    ],
}


def _migrate_phase2(bind) -> None:
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    with bind.begin() as conn:
        for table, columns in _PHASE2_COLUMNS.items():
            if table not in existing_tables:
                continue
            present = {c["name"] for c in inspector.get_columns(table)}
            for col_name, col_type in columns:
                if col_name in present:
                    continue
                conn.execute(text(
                    f'ALTER TABLE {table} ADD COLUMN {col_name} {col_type}'
                ))
                logger.info("Migrated %s: added column %s", table, col_name)


def init_db():
    """Create tables if they don't exist, then apply Phase 2 column adds."""
    from app.models import db_models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _migrate_phase2(engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
