# Architecture

## Data flow

```
Folder of images
      │
      ▼
[1] Ingest    ─────►  Copy/reference images into a session.
                       Extract EXIF (DateTimeOriginal, copyright field).
                       Persist Image rows to SQLite.
      │
      ▼
[2] Detect    ─────►  InsightFace runs on every image.
                       Each detected face → (bbox, embedding, image_id).
                       Persist Face rows.
      │
      ▼
[3] Cluster   ─────►  Group Faces by embedding similarity (DBSCAN or
                       agglomerative on cosine distance).
                       One cluster ≈ one player.
                       Persist Cluster rows + assign each Face a cluster_id.
      │
      ▼
[4] Classify  ─────►  For each Image:
                         - face_count (from [2])
                         - expression per face (smile vs serious)
                       Persist ImageClassification rows.
      │
      ▼
[5] Sort      ─────►  For each Cluster, apply sort_rules.py to assign each
                       image a role: team | panoramic | individual | buddy.
                       Persist ImageRole rows.
      │
      ▼
[6] Flag      ─────►  Compute median photos/cluster. Mark outliers and
                       rule-failure clusters as needs_review.
      │
      ▼
[7] Review UI  ────►  React dashboard: cluster grid with thumbnails,
                       roles visible per image, drag-to-reassign,
                       merge clusters, create new cluster.
```

Steps 1–6 are the **pipeline**. Step 7 is the **UI**. When the user reassigns in step 7, only steps 5–6 re-run for the affected clusters (fast).

## Components

### `backend/app/services/face_pipeline.py`

Orchestrates steps 1–6. Single entrypoint: `run_pipeline(session_id)`.

### `backend/app/services/face_detector.py`

Thin wrapper around InsightFace. Returns `[(bbox, embedding, det_score), ...]` per image. Loads the model once at process start.

### `backend/app/services/cluster.py`

Takes a list of embeddings, returns cluster labels. Default: DBSCAN with cosine metric, `eps=0.4`, `min_samples=2`. Tunable.

### `backend/app/services/expression.py`

Smile vs serious classifier. Start with a simple pretrained model (e.g. a small CNN or the `py-feature` library); the agent can swap in something better later. Returns `{"smiling": float, "serious": float}` confidence per face crop.

### `backend/app/services/sort_rules.py`

Pure functions, no I/O. Input: list of `Image` records for one cluster with classifications. Output: dict of `image_id -> role`. **This file is the canonical source of the sorting logic** — see rules in README.

### `backend/app/services/outliers.py`

Computes the median and flags clusters per the rules in README.

### `backend/app/api/`

FastAPI routers:

- `POST   /api/sessions`                       create session, accepts folder path
- `POST   /api/sessions/{id}/run`              kick off pipeline
- `GET    /api/sessions/{id}`                  session status + summary
- `GET    /api/sessions/{id}/clusters`         clusters with thumbnails + roles
- `POST   /api/sessions/{id}/reassign`         move images between clusters
- `POST   /api/sessions/{id}/merge-clusters`   merge two clusters
- `POST   /api/sessions/{id}/new-cluster`      create an empty cluster, returns id
- `GET    /api/images/{id}/thumb`              JPEG thumbnail
- `GET    /api/images/{id}/full`               full-res JPEG

### Data model (SQLite)

```sql
CREATE TABLE sessions (
  id INTEGER PRIMARY KEY,
  name TEXT,
  source_path TEXT,
  status TEXT,             -- pending | running | done | error
  created_at DATETIME,
  pipeline_finished_at DATETIME
);

CREATE TABLE images (
  id INTEGER PRIMARY KEY,
  session_id INTEGER REFERENCES sessions(id),
  path TEXT,
  filename TEXT,
  capture_time DATETIME,   -- from EXIF, fallback to filename sort key
  copyright_tag TEXT       -- from EXIF if present
);

CREATE TABLE faces (
  id INTEGER PRIMARY KEY,
  image_id INTEGER REFERENCES images(id),
  bbox TEXT,               -- JSON [x,y,w,h]
  embedding BLOB,          -- float32 array
  det_score REAL,
  expression TEXT,         -- smiling | serious | unknown
  expression_score REAL,
  cluster_id INTEGER REFERENCES clusters(id)
);

CREATE TABLE clusters (
  id INTEGER PRIMARY KEY,
  session_id INTEGER REFERENCES sessions(id),
  label TEXT,              -- "Player 1" until user renames; can store roster name later
  needs_review INTEGER,    -- 0/1
  review_reason TEXT,      -- "outlier_high" | "outlier_low" | "no_team_pick" | etc
  image_count INTEGER
);

CREATE TABLE image_roles (
  image_id INTEGER REFERENCES images(id),
  cluster_id INTEGER REFERENCES clusters(id),
  role TEXT,               -- team | panoramic | individual | buddy | excluded
  PRIMARY KEY (image_id, cluster_id)
);
```

`faces.cluster_id` is the live assignment. `image_roles` is denormalized per-cluster because an image with multiple faces appears in multiple clusters.

### Frontend

Vite + React. Two pages:

- `/` — session list and "new session" form
- `/session/:id` — cluster grid. Each cluster card shows:
  - Cluster label, image count, `needs_review` badge if set
  - Thumbnail strip in capture order with role badges (team/pano/ind/buddy)
  - Drag handle on each thumbnail to reassign to another cluster
  - "Merge into…" and "Rename" actions per cluster
  - "Create new cluster" button at the top
