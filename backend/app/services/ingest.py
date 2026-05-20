"""Image ingestion: scan a folder, extract EXIF, create Image rows.

Supports .jpg/.jpeg/.png/.tif/.tiff. Reads:
  - DateTimeOriginal → Image.capture_time
  - Copyright       → Image.copyright_tag

Falls back to filename for ordering if EXIF is missing.

Format note: PNG metadata can live in tEXt/iTXt/zTXt chunks, an embedded eXIf
chunk, or an XMP packet depending on the software that wrote it. `exifread`
only handles JPEG/TIFF, so the PNG path uses Pillow and checks all three
locations (see _read_png_metadata). JPEG/TIFF stay on the exifread path.
"""
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Iterable

import exifread
from PIL import Image as PILImage

from sqlalchemy.orm import Session as DbSession
from app.models.db_models import Image, Session

logger = logging.getLogger(__name__)


SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
RAW_EXTS = {".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2"}

# Phase 7: EXIF reads are pure I/O over the UNC share; threads beat the
# sequential per-file round-trips by ~5–10x on a typical SMB volume.
_EXIF_WORKERS = 8


def ingest_folder(db: DbSession, session_id: int, folder: Path) -> int:
    """Scan `folder` and create Image rows for the session. Returns count added.

    EXIF / PNG-metadata reads run on a ThreadPoolExecutor — per-file cost is
    dominated by network I/O over UNC shares, so threads (not processes) are
    the right hammer. SQLAlchemy session writes stay on the main thread
    (Session isn't thread-safe); workers only return value tuples which the
    main thread fans into Image rows and commits in a single transaction.
    """
    session = db.query(Session).get(session_id)
    if session is None:
        raise ValueError(f"Session {session_id} not found")

    start = time.monotonic()
    paths = [
        p for p in sorted(folder.iterdir())
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
    ]
    if not paths:
        db.commit()
        logger.info("[ingest] team %s: 0 files in %.1fs",
                    session.name, time.monotonic() - start)
        return 0

    # executor.map preserves input order, so Image rows land in folder order.
    with ThreadPoolExecutor(max_workers=_EXIF_WORKERS) as ex:
        exifs = list(ex.map(_read_exif, paths))

    db.add_all([
        Image(
            session_id=session_id,
            path=str(path.resolve()),
            filename=path.name,
            capture_time=capture_time,
            copyright_tag=copyright_tag,
        )
        for path, (capture_time, copyright_tag) in zip(paths, exifs)
    ])
    db.commit()
    logger.info("[ingest] team %s: %d files in %.1fs",
                session.name, len(paths), time.monotonic() - start)
    return len(paths)


def _read_exif(path: Path) -> tuple[datetime | None, str | None]:
    """Pull capture time and copyright tag from the image's metadata.

    Dispatches to format-specific readers because PNG stores metadata in text
    chunks (not EXIF). Returns (capture_time, copyright_tag); either may be
    None if absent or unparseable.
    """
    suffix = path.suffix.lower()
    if suffix == ".png":
        return _read_png_metadata(path)
    return _read_exif_metadata(path)


def _read_exif_metadata(path: Path) -> tuple[datetime | None, str | None]:
    """JPEG/TIFF path via exifread."""
    try:
        with open(path, "rb") as f:
            tags = exifread.process_file(f, details=False)
    except (OSError, ValueError) as exc:
        logger.warning("Could not read EXIF for %s: %s", path, exc)
        return None, None

    capture_time: datetime | None = None
    dt = tags.get("EXIF DateTimeOriginal") or tags.get("Image DateTime")
    if dt:
        try:
            capture_time = datetime.strptime(str(dt), "%Y:%m:%d %H:%M:%S")
        except ValueError:
            logger.warning("Unparseable DateTimeOriginal %r for %s", str(dt), path)

    copyright_raw = tags.get("Image Copyright") or tags.get("EXIF Copyright")
    copyright_tag = str(copyright_raw) if copyright_raw else None

    return capture_time, copyright_tag


_XMP_RIGHTS_RE = re.compile(
    r"<(?:dc:)?rights>.*?<rdf:li[^>]*>(.*?)</rdf:li>", re.DOTALL | re.IGNORECASE,
)
_XMP_RIGHTS_SIMPLE_RE = re.compile(
    r"<(?:dc:)?rights>(?:\s*)([^<]+?)(?:\s*)</(?:dc:)?rights>", re.IGNORECASE,
)


def _clean(value) -> str | None:
    """Stringify, strip, return None for empty."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _read_png_metadata(path: Path) -> tuple[datetime | None, str | None]:
    """Pull capture time + copyright from a PNG.

    Phase 4.4: the original implementation only read img.info (PNG tEXt/zTXt
    chunks). Real Sportsline files are Canon RAWs converted to PNG — the
    copyright the photographer set is carried in the PNG eXIf chunk and/or an
    XMP packet, NEVER a plain tEXt "Copyright" chunk. Windows Explorer shows it
    (it reads EXIF+XMP); img.info doesn't. So every PNG returned (None, None)
    and every cluster stayed "Player N".

    Now we check, in priority order:
      1. tEXt/iTXt chunks via img.info / img.text  ("Copyright", "copyright")
      2. embedded EXIF via img.getexif()           (tag 33432 Copyright,
                                                     36867 DateTimeOriginal,
                                                     306 DateTime)
      3. XMP packet (dc:rights / photoshop:Copyright) parsed out of the
         iTXt "XML:com.adobe.xmp" chunk
    """
    try:
        with PILImage.open(path) as img:
            info = dict(img.info or {})
            text = dict(getattr(img, "text", {}) or {})
            try:
                exif = img.getexif()
            except Exception:  # noqa: BLE001 — some PNGs have malformed eXIf
                exif = {}
    except (OSError, PILImage.UnidentifiedImageError) as exc:
        logger.warning("Could not read PNG metadata for %s: %s", path, exc)
        return None, None

    # 1 — text chunks
    copyright_tag = _clean(
        info.get("Copyright") or info.get("copyright")
        or text.get("Copyright") or text.get("copyright")
    )

    # 2 — embedded EXIF (Copyright = 33432)
    if copyright_tag is None and exif:
        copyright_tag = _clean(exif.get(33432))

    # 3 — XMP packet
    if copyright_tag is None:
        xmp_raw = (
            info.get("XML:com.adobe.xmp") or info.get("xmp")
            or text.get("XML:com.adobe.xmp")
        )
        if xmp_raw:
            xmp = xmp_raw.decode("utf-8", "ignore") if isinstance(xmp_raw, bytes) else str(xmp_raw)
            m = _XMP_RIGHTS_RE.search(xmp) or _XMP_RIGHTS_SIMPLE_RE.search(xmp)
            if m:
                copyright_tag = _clean(m.group(1))

    # Capture time: PNG "Creation Time" text chunk, else EXIF DateTimeOriginal.
    capture_time: datetime | None = None
    ct_raw = (
        info.get("Creation Time") or info.get("creation time")
        or text.get("Creation Time")
    )
    if ct_raw:
        capture_time = _parse_loose_datetime(str(ct_raw))
    if capture_time is None and exif:
        dt_raw = exif.get(36867) or exif.get(306)  # DateTimeOriginal / DateTime
        if dt_raw:
            capture_time = _parse_loose_datetime(str(dt_raw))

    return capture_time, copyright_tag


def _parse_loose_datetime(raw: str) -> datetime | None:
    """Try common datetime formats. Return None if nothing matches."""
    raw = raw.strip()
    if not raw:
        return None
    formats = [
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y:%m:%d %H:%M:%S",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None
