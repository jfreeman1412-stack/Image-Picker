"""Tests for image ingestion metadata readers.

Focus is on the PNG path added in Phase 4.1 — exifread can't see PNG text
chunks, so Pillow takes over for .png files.
"""
from datetime import datetime
from pathlib import Path

from PIL import Image as PILImage, PngImagePlugin

from app.services.ingest import (
    _parse_loose_datetime, _read_exif, _read_png_metadata,
)


def _make_png_with_metadata(path: Path, **text_chunks):
    """Write a tiny PNG with the given tEXt chunks."""
    img = PILImage.new("RGB", (10, 10), color="white")
    info = PngImagePlugin.PngInfo()
    for key, value in text_chunks.items():
        if value is not None:
            info.add_text(key, value)
    img.save(path, "PNG", pnginfo=info)


def _make_png_with_itxt(path: Path, key: str, value: str):
    """PNG whose copyright is in an iTXt chunk (language-tagged) — how some
    editors (and the real Sportsline conversion path) write it."""
    img = PILImage.new("RGB", (10, 10), color="white")
    info = PngImagePlugin.PngInfo()
    info.add_itxt(key, value, lang="en", tkey=key)
    img.save(path, "PNG", pnginfo=info)


def _make_png_with_exif_chunk(path: Path, copyright_value: str):
    """PNG carrying an embedded eXIf chunk with the Copyright tag (33432) —
    this is what camera RAW → PNG conversions produce, and what Windows
    Explorer reads. img.info does NOT surface this."""
    img = PILImage.new("RGB", (10, 10), color="white")
    exif = PILImage.Exif()
    exif[33432] = copyright_value          # Copyright
    img.save(path, "PNG", exif=exif)


def _make_png_with_xmp(path: Path, rights: str):
    """PNG with copyright only in an XMP packet (dc:rights)."""
    xmp = (
        '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:rights><rdf:Alt><rdf:li xml:lang="x-default">{rights}</rdf:li>'
        '</rdf:Alt></dc:rights>'
        '</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="r"?>'
    )
    img = PILImage.new("RGB", (10, 10), color="white")
    info = PngImagePlugin.PngInfo()
    info.add_itxt("XML:com.adobe.xmp", xmp, lang="", tkey="")
    img.save(path, "PNG", pnginfo=info)


def test_png_copyright_read(tmp_path):
    path = tmp_path / "shot.png"
    _make_png_with_metadata(path, Copyright="Quinn-Gentz")
    capture_time, copyright_tag = _read_exif(path)
    assert capture_time is None
    assert copyright_tag == "Quinn-Gentz"


def test_png_lowercase_copyright_key(tmp_path):
    path = tmp_path / "shot.png"
    _make_png_with_metadata(path, copyright="Lowercase Studio")
    _, copyright_tag = _read_exif(path)
    assert copyright_tag == "Lowercase Studio"


def test_png_no_metadata_clean(tmp_path):
    path = tmp_path / "bare.png"
    _make_png_with_metadata(path)  # nothing
    capture_time, copyright_tag = _read_exif(path)
    assert capture_time is None
    assert copyright_tag is None


# ── Phase 4.4 regressions: copyright not in a plain tEXt chunk ────────────────


def test_png_copyright_from_embedded_exif_chunk(tmp_path):
    """Camera RAW → PNG conversions put Copyright in an eXIf chunk, not tEXt.
    This is the actual Sportsline failure mode — img.info never had it."""
    path = tmp_path / "fromcam.png"
    _make_png_with_exif_chunk(path, "Quinn-Gentz")
    _, copyright_tag = _read_exif(path)
    assert copyright_tag == "Quinn-Gentz"


def test_png_copyright_from_itxt_chunk(tmp_path):
    """Some editors write Copyright as a language-tagged iTXt chunk."""
    path = tmp_path / "itxt.png"
    _make_png_with_itxt(path, "Copyright", "Smith Photography")
    _, copyright_tag = _read_exif(path)
    assert copyright_tag == "Smith Photography"


def test_png_copyright_from_xmp_packet(tmp_path):
    """Copyright only present in an XMP dc:rights block."""
    path = tmp_path / "xmp.png"
    _make_png_with_xmp(path, "Garcia Studios 2026")
    _, copyright_tag = _read_exif(path)
    assert copyright_tag == "Garcia Studios 2026"


def test_png_tEXt_still_preferred_when_present(tmp_path):
    """Don't regress the original tEXt path."""
    path = tmp_path / "text.png"
    _make_png_with_metadata(path, Copyright="Plain Text Co")
    _, copyright_tag = _read_exif(path)
    assert copyright_tag == "Plain Text Co"


def test_png_creation_time_iso(tmp_path):
    path = tmp_path / "iso.png"
    _make_png_with_metadata(path, **{"Creation Time": "2025-03-15T10:23:00"})
    capture_time, _ = _read_exif(path)
    assert capture_time == datetime(2025, 3, 15, 10, 23, 0)


def test_png_creation_time_space_separated(tmp_path):
    path = tmp_path / "space.png"
    _make_png_with_metadata(path, **{"Creation Time": "2025-03-15 10:23:00"})
    capture_time, _ = _read_exif(path)
    assert capture_time == datetime(2025, 3, 15, 10, 23, 0)


def test_png_creation_time_malformed_returns_none(tmp_path):
    path = tmp_path / "junk.png"
    _make_png_with_metadata(path, **{"Creation Time": "yesterday"})
    capture_time, _ = _read_exif(path)
    assert capture_time is None  # no crash, just None


def test_png_empty_string_copyright_treated_as_missing(tmp_path):
    path = tmp_path / "empty.png"
    _make_png_with_metadata(path, Copyright="   ")
    _, copyright_tag = _read_exif(path)
    assert copyright_tag is None


def test_png_unreadable_file_returns_nones(tmp_path):
    path = tmp_path / "broken.png"
    path.write_bytes(b"not a png")
    capture_time, copyright_tag = _read_exif(path)
    assert capture_time is None
    assert copyright_tag is None


# ── _parse_loose_datetime unit tests ──────────────────────────────────────────


def test_parse_loose_iso():
    assert _parse_loose_datetime("2025-03-15T10:23:00") == datetime(2025, 3, 15, 10, 23, 0)


def test_parse_loose_exif_colons():
    assert _parse_loose_datetime("2025:03:15 10:23:00") == datetime(2025, 3, 15, 10, 23, 0)


def test_parse_loose_date_only():
    assert _parse_loose_datetime("2025-03-15") == datetime(2025, 3, 15, 0, 0, 0)


def test_parse_loose_garbage_is_none():
    assert _parse_loose_datetime("not a date") is None
    assert _parse_loose_datetime("") is None
    assert _parse_loose_datetime("   ") is None


# ── JPEG fallback still works ─────────────────────────────────────────────────


def test_jpeg_dispatches_to_exifread_path(tmp_path):
    """A JPEG with no EXIF should return (None, None) via the exifread path
    without raising — proves the dispatch routes JPEGs correctly."""
    path = tmp_path / "bare.jpg"
    img = PILImage.new("RGB", (10, 10), color="white")
    img.save(path, "JPEG")
    capture_time, copyright_tag = _read_exif(path)
    assert capture_time is None
    assert copyright_tag is None


def test_read_png_metadata_direct_call(tmp_path):
    """Direct call to the PNG helper bypasses the dispatch."""
    path = tmp_path / "direct.png"
    _make_png_with_metadata(path, Copyright="DirectCall")
    _, copyright_tag = _read_png_metadata(path)
    assert copyright_tag == "DirectCall"
