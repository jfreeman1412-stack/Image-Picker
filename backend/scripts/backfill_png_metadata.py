"""One-shot backfill for PNG copyright tags missed by the old exifread path.

Run from the backend/ directory:

    python -m scripts.backfill_png_metadata

Updates Image rows where path ends in .png and copyright_tag IS NULL. Reads
the PNG text chunks via Pillow and writes the result back to the DB. Doesn't
touch JPEG/TIFF rows.

After this finishes, cluster labels don't refresh automatically — re-run the
pipeline on each session (or just the relabeling step) to pick up the new
tags.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from app.db import SessionLocal
from app.models.db_models import Image
from app.services.ingest import _read_png_metadata

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("backfill")


def main() -> int:
    db = SessionLocal()
    try:
        rows = (
            db.query(Image)
            .filter(Image.copyright_tag.is_(None))
            .filter(Image.path.ilike("%.png"))
            .all()
        )
        log.info("Found %d PNG rows with NULL copyright_tag", len(rows))

        updated = 0
        missing_file = 0
        for img in rows:
            p = Path(img.path)
            if not p.exists():
                missing_file += 1
                continue
            capture_time, copyright_tag = _read_png_metadata(p)
            if copyright_tag:
                img.copyright_tag = copyright_tag
                updated += 1
            # Don't overwrite an existing capture_time; only fill it if NULL.
            if capture_time and img.capture_time is None:
                img.capture_time = capture_time
        db.commit()

        log.info("Updated %d rows with copyright_tag from PNG text chunks", updated)
        if missing_file:
            log.warning("%d rows skipped — source file missing on disk", missing_file)
        log.info("Done. Re-run the pipeline on affected sessions to refresh cluster labels.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
