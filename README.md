# Player Sort

Auto-sorts volume sports photography sessions by player using facial recognition.

## What it does

Given a folder of images from a single shoot session, the app:

1. Detects and embeds every face in every image using InsightFace.
2. Clusters embeddings into one cluster per player.
3. For each cluster, classifies each image as **single-face** vs **multi-face (buddy)** and **smiling** vs **serious**.
4. Applies the studio sorting rules:
   - **Team photo** = the *last* single-face, smiling image in the cluster's capture order.
   - **Panoramic photo** = the *last* single-face, serious image *before* the team photo (i.e. second-to-last serious-single).
   - **Individuals / keepers** = everything else that's good (including buddy shots).
5. Flags outlier clusters (e.g. one kid with 20 photos when the median is 8) for manual review.
6. Provides a React review UI for reassigning images between clusters, merging clusters, or creating a new player cluster.

## Architecture

```
player-sort/
├── backend/      Python FastAPI service. InsightFace + SQLite. Port 8020.
├── frontend/     React (Vite) UI. Port 8021.
```

The backend runs the CV pipeline and exposes a REST API. The frontend is a thin client that displays clusters and lets you reassign. SQLite stores cluster assignments, image metadata, and classification results per session.

This is designed to eventually plug into the Sportsline Production Dashboard, but it runs standalone for now.

## Quick start (handing to Claude Code)

This repo is a **scaffold**, not a finished app. Files are stubbed with clear `TODO` markers and inline notes describing what each piece should do. Hand this folder to Claude Code with a prompt like:

> "Read README.md and ARCHITECTURE.md, then implement the TODOs in order. Start with backend/app/services/face_pipeline.py. Run the smoke test at backend/tests/test_pipeline.py against backend/data/uploads/sample_session/ when one exists."

### Backend setup

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate         # Windows
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8020
```

### Frontend setup

```bash
cd frontend
npm install
npm run dev   # opens on 8021
```

### Reaching the app from another machine on your LAN

Both servers are configured to bind to all network interfaces by default. To
expose the dev environment to the LAN:

```bash
# backend
uvicorn app.main:app --reload --host 0.0.0.0 --port 8020

# frontend (vite.config.js already sets host: true, so plain `npm run dev`
# is enough — confirm the terminal output prints a "Network:" URL)
npm run dev
```

Then from another machine on the same network, browse to:

```
http://<host-pc-lan-ip>:8021
```

Find the LAN IP with `ipconfig` on Windows (look for IPv4 under the active
adapter, e.g. `192.168.x.x`). The first time uvicorn/node bind to a
non-loopback interface, Windows Defender Firewall will prompt to allow them
— accept on Private networks.

CORS is restricted to localhost + RFC1918 (10/8, 172.16/12, 192.168/16)
origins, so this only opens up your LAN. Don't run it on a public network
— there's no auth and creating jobs lets the caller point at arbitrary
folder paths on the host.

## Sorting rules (the source of truth)

These are the rules the pipeline implements. If shooting conventions change, edit `backend/app/services/sort_rules.py` and nowhere else.

1. Group images by face cluster (one cluster ≈ one player).
2. Within a cluster, sort images by capture timestamp (EXIF `DateTimeOriginal`, fallback to filename order).
3. Filter to **single-face** images for the team/pano picks. Multi-face images are always "individuals/keepers."
4. **Team** = last single-face image classified as `smiling`.
5. **Panoramic** = last single-face image classified as `serious`, occurring *before* the team photo in the timeline.
6. If rules 4 or 5 can't be satisfied (e.g. no smiling single-face image exists), mark the cluster as `needs_review` and surface it in the UI.

## Outlier detection

A cluster is flagged `needs_review` if:

- Image count is > 1.5x median across clusters in the session, OR
- Image count is < 0.5x median, OR
- Team or panoramic pick failed per rules above, OR
- The cluster contains an image with no detected face but the user has manually added it.

## Rosters

There are **two** roster CSVs, for two different purposes — both reached from a
job's page in the desktop app:

- **Player roster** (the *"Player roster"* button) — the per-shoot list of
  players the reference-photo capture + face-matching system uses. Rosters arrive
  in many column layouts, so you **map the CSV's columns** to the canonical
  fields (**name** + **team**; other columns such as parent contact are ignored),
  the app validates strictly, and on success the roster is written for the shoot.
  Name can be one full-name column *or* separate first/last columns (a
  last-name-only row is valid). Re-uploading **replaces** the shoot's roster; if
  reference photos were already captured for that shoot you're asked to confirm.
  Coaches are auto-flagged from a `Coach-` name prefix.
- **Roster cross-check** (the *"Roster cross-check"* button) — a positional
  `name,team` CSV (no header) used *after* the pipeline runs to flag clusters
  that landed on the wrong team. A separate system writing a separate table.

Sample player-roster CSVs to try are in [`sample-rosters/`](sample-rosters/):
`full-name.csv` (one name column + ignored PII columns), `first-last.csv`
(separate first/last; includes a last-name-only row and a coach), and
`missing-team.csv` (two rows missing a team — the upload is blocked and the
error names the offending rows).

## What this scaffold does NOT include (yet)

- Eyes-open / looking-away quality scoring (v2 — building sort first per Joey's call).
- Integration with Sytist or the Sportsline Production Dashboard (plug-in later).
- Authentication. Runs as a local tool.

See `ARCHITECTURE.md` for component details and `backend/app/services/face_pipeline.py` for the CV pipeline outline.
