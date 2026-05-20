"""SQLAlchemy ORM models. Schema is the source of truth in ARCHITECTURE.md."""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, DateTime, Float, ForeignKey, Index, LargeBinary,
    PrimaryKeyConstraint,
)
from sqlalchemy.orm import relationship

from app.db import Base


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True)
    name = Column(String)
    root_path = Column(String)                              # the main job folder
    has_lines = Column(Integer, default=0)                  # 0 | 1
    image_subfolder_name = Column(String, nullable=True)    # e.g. "JPG"; None = JPGs in team folder root
    created_at = Column(DateTime, default=datetime.utcnow)
    archived = Column(Integer, default=0)                   # 0 | 1; soft-hide from default list
    archived_at = Column(DateTime, nullable=True)
    ingest_status = Column(String, default="pending")       # pending | ingesting | done | error
    ingest_progress = Column(Integer, default=0)            # teams ingested so far
    ingest_total = Column(Integer, default=0)               # total teams to ingest
    ingest_current_team = Column(String, nullable=True)     # team currently being processed
    ingest_error = Column(String, nullable=True)
    # Export progress (async, polled by the export modal).
    export_status = Column(String, default="idle")          # idle | exporting | done | error
    export_progress = Column(Integer, default=0)            # files copied so far
    export_total = Column(Integer, default=0)               # files to copy
    export_current_team = Column(String, nullable=True)
    export_started_at = Column(DateTime, nullable=True)     # for ETA
    export_error = Column(String, nullable=True)
    export_result = Column(String, nullable=True)           # JSON: final stats

    sessions = relationship("Session", back_populates="job", cascade="all, delete-orphan")
    roster_entries = relationship(
        "RosterEntry", back_populates="job", cascade="all, delete-orphan"
    )


class Session(Base):
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True)  # NULL = legacy/standalone
    name = Column(String)                       # team name (e.g. "11a Eagles")
    source_path = Column(String)                # the team folder path
    status = Column(String, default="pending")  # pending | running | done | error
    created_at = Column(DateTime, default=datetime.utcnow)
    pipeline_finished_at = Column(DateTime, nullable=True)
    reviewed = Column(Integer, default=0)       # 0 | 1; user-facing "I'm done with this team"
    reviewed_at = Column(DateTime, nullable=True)
    archived = Column(Integer, default=0)            # 0 | 1; soft-hide from job detail
    archived_at = Column(DateTime, nullable=True)
    progress_stage = Column(String, nullable=True)   # "detecting" | "clustering" | "classifying" | "sorting" | None
    progress_current = Column(Integer, default=0)
    progress_total = Column(Integer, default=0)      # 0 → indeterminate (just show stage name)
    progress_started_at = Column(DateTime, nullable=True)        # whole-pipeline start (for elapsed)
    progress_stage_started_at = Column(DateTime, nullable=True)  # current-stage start (for ETA)

    job = relationship("Job", back_populates="sessions")
    images = relationship("Image", back_populates="session", cascade="all, delete-orphan")
    clusters = relationship("Cluster", back_populates="session", cascade="all, delete-orphan")


class Image(Base):
    __tablename__ = "images"

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("sessions.id"))
    path = Column(String)
    filename = Column(String)
    capture_time = Column(DateTime, nullable=True)
    copyright_tag = Column(String, nullable=True)

    session = relationship("Session", back_populates="images")
    faces = relationship("Face", back_populates="image", cascade="all, delete-orphan")


class Face(Base):
    __tablename__ = "faces"

    id = Column(Integer, primary_key=True)
    image_id = Column(Integer, ForeignKey("images.id"))
    bbox = Column(String)  # JSON [x, y, w, h]
    embedding = Column(LargeBinary)  # numpy float32 .tobytes()
    det_score = Column(Float)
    expression = Column(String, nullable=True)  # smiling | serious | unknown
    expression_score = Column(Float, nullable=True)
    age = Column(Float, nullable=True)  # InsightFace age estimate
    yaw = Column(Float, nullable=True)              # head pose yaw, degrees
    pitch = Column(Float, nullable=True)            # head pose pitch, degrees
    face_area_ratio = Column(Float, nullable=True)  # face bbox area / image area
    cluster_id = Column(Integer, ForeignKey("clusters.id"), nullable=True)

    image = relationship("Image", back_populates="faces")
    cluster = relationship("Cluster", back_populates="faces")


class Cluster(Base):
    __tablename__ = "clusters"

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("sessions.id"))
    label = Column(String)  # DEPRECATED — kept for back-compat; reads use manual_label or auto_label
    needs_review = Column(Integer, default=0)
    review_reason = Column(String, nullable=True)
    image_count = Column(Integer, default=0)
    is_likely_coach = Column(Integer, default=0)            # 0 | 1; auto-detected
    auto_label = Column(String, nullable=True)              # from copyright EXIF
    manual_label = Column(String, nullable=True)            # set by rename; wins over auto_label
    # 0 = no override (use is_likely_coach), 1 = forced coach, -1 = forced player
    manual_coach_override = Column(Integer, default=0)

    session = relationship("Session", back_populates="clusters")
    faces = relationship("Face", back_populates="cluster")

    def display_label(self) -> str:
        return self.manual_label or self.auto_label or f"Player {self.id}"

    def is_coach_for_sort(self) -> bool:
        """Effective coach state for sort dispatch: manual override wins,
        otherwise fall back to auto-detected is_likely_coach."""
        if self.manual_coach_override == 1:
            return True
        if self.manual_coach_override == -1:
            return False
        return bool(self.is_likely_coach)


class ImageRole(Base):
    __tablename__ = "image_roles"
    __table_args__ = (PrimaryKeyConstraint("image_id", "cluster_id"),)

    image_id = Column(Integer, ForeignKey("images.id"))
    cluster_id = Column(Integer, ForeignKey("clusters.id"))
    role = Column(String)  # team | panoramic | individual | buddy | rejected
    manual_override = Column(Integer, default=0)  # 0 | 1; if 1, pipeline must not overwrite


class RosterEntry(Base):
    """One row per (job, player) from the per-job roster CSV. Same CSV the
    photographer used to write copyright tags into the camera, so matching
    Cluster.auto_label against norm_name is strict equality after
    normalize_name(). norm_team matches normalize_name(Session.name)."""
    __tablename__ = "roster_entries"
    __table_args__ = (
        Index("ix_roster_entries_job_norm_name", "job_id", "norm_name"),
    )

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False)
    raw_name = Column(String, nullable=False)    # "Eleanor-Pederson" — as in CSV
    norm_name = Column(String, nullable=False)   # "eleanorpederson" — lookup key
    team_name = Column(String, nullable=False)   # "10U-Black-Softball" — as in CSV
    norm_team = Column(String, nullable=False)   # "10ublacksoftball" — matches norm(session.name)

    job = relationship("Job", back_populates="roster_entries")


class Setting(Base):
    """Generic key/value store for per-installation preferences (single-user
    studio — not per-user). Values are JSON-encoded strings."""
    __tablename__ = "settings"

    key = Column(String, primary_key=True)
    value = Column(String)  # JSON-encoded
