"""Read-only access to the Sytist MySQL database (2026-10-08).

Player Sort pulls families for a passcode job from two Sytist tables:

  - ms_bookings: booking-calendar sign-ups. book_special_event_id is the
    booking session (ms_booking_special_dates.sd_id); that session's
    sd_date_id is the booking page (ms_calendar.date_id), whose date_title
    is the name people see. book_first/last_name,
    book_email, book_phone are the parent; book_subject_first/last_name the
    player.
  - ms_pre_register: gallery pre-registrations. reg_date_id is the gallery
    (ms_calendar.date_id); reg_first/last_name, reg_email, reg_phone are the
    parent; reg_subject_first/last_name the player. Rows with a
    reg_group_value are group photos made by the passcode import, not people.

Nothing here writes to Sytist. Every connection opens a READ ONLY session,
the same rule the production dashboard and digital-delivery apps follow.

Connection settings come from the `sytist_db` setting (Settings screen),
falling back to the SYTIST_DB_HOST / _PORT / _USER / _PASSWORD / _NAME
environment variables the other shop apps use.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session as DbSession

from app.models.db_models import Setting

SETTING_KEY = "sytist_db"
CONFIG_FIELDS = ("host", "port", "user", "password", "database")
DEFAULT_PORT = 3306
DEFAULT_DATABASE = "sportsline"
CONNECT_TIMEOUT_S = 10


class SytistDbError(RuntimeError):
    """Sytist can't be reached or isn't set up. The message is shown as is."""


def load_config(db: DbSession) -> dict:
    stored: dict = {}
    row = db.query(Setting).get(SETTING_KEY)
    if row and row.value:
        try:
            stored = json.loads(row.value) or {}
        except (ValueError, TypeError):
            stored = {}
    env = {
        "host": os.environ.get("SYTIST_DB_HOST"),
        "port": os.environ.get("SYTIST_DB_PORT"),
        "user": os.environ.get("SYTIST_DB_USER"),
        "password": os.environ.get("SYTIST_DB_PASSWORD"),
        "database": os.environ.get("SYTIST_DB_NAME"),
    }
    cfg = {f: stored.get(f) or env.get(f) or "" for f in CONFIG_FIELDS}
    cfg["port"] = int(cfg["port"] or DEFAULT_PORT)
    cfg["database"] = cfg["database"] or DEFAULT_DATABASE
    return cfg


def save_config(db: DbSession, values: dict) -> dict:
    """Store connection settings. A blank password keeps the stored one, so
    the Settings screen never has to show it. Caller commits."""
    current = {}
    row = db.query(Setting).get(SETTING_KEY)
    if row and row.value:
        try:
            current = json.loads(row.value) or {}
        except (ValueError, TypeError):
            current = {}
    for f in CONFIG_FIELDS:
        if f not in values or values[f] is None:
            continue
        if f == "password" and values[f] == "":
            continue
        current[f] = values[f]
    if row is None:
        row = Setting(key=SETTING_KEY)
        db.add(row)
    row.value = json.dumps(current)
    return public_config(db)


def public_config(db: DbSession) -> dict:
    cfg = load_config(db)
    return {
        "host": cfg["host"], "port": cfg["port"], "user": cfg["user"],
        "database": cfg["database"], "password_set": bool(cfg["password"]),
        "configured": bool(cfg["host"] and cfg["user"]),
    }


@contextmanager
def connect(db: DbSession):
    """A read-only pymysql connection to Sytist."""
    cfg = load_config(db)
    if not (cfg["host"] and cfg["user"]):
        raise SytistDbError(
            "Sytist database isn't set up. Add the host, user and password "
            "under Settings > Sytist.")
    try:
        import pymysql
        import pymysql.cursors
    except ImportError as exc:  # pragma: no cover - install problem
        raise SytistDbError("The pymysql package isn't installed.") from exc
    try:
        conn = pymysql.connect(
            host=cfg["host"], port=cfg["port"], user=cfg["user"],
            password=cfg["password"], database=cfg["database"],
            charset="utf8mb4", connect_timeout=CONNECT_TIMEOUT_S,
            read_timeout=60, cursorclass=pymysql.cursors.DictCursor,
        )
    except Exception as exc:
        raise SytistDbError(f"Couldn't connect to Sytist: {exc}") from exc
    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
        yield conn
    finally:
        conn.close()


def _query(conn, sql: str, params=()) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def _placeholders(values) -> str:
    return ", ".join(["%s"] * len(values))


def _event_title(row: dict) -> str:
    """Booking page name, plus the session name when it has one, e.g.
    "Revolution Gymnastics Photo Day Sign Up-2026 · Tues AM Sessions"."""
    page = (row.get("page_title") or "").strip()
    session = (row.get("session_title") or "").strip()
    if page and session:
        return f"{page} · {session}"
    return page or session or f"Event {row['id']}"


# Booking forms usually ask for the kid's name as a custom question
# ("Gymnast's Name:", "Skaters Name:", "Player Name", "Athlete Name"...)
# instead of filling book_subject_first/last_name. book_options holds the
# answers, one per line as "Question|Answer|price".
_NOT_THE_KID = ("parent", "guardian", "coach", "team", "sponsor", "studio",
                "association", "contact", "mother", "father")


def subject_from_options(options) -> tuple[str | None, str | None]:
    """The kid's (first, last) name from a booking's custom form answers,
    or (None, None) when no name question was answered."""
    for line in str(options or "").splitlines():
        parts = line.split("|")
        if len(parts) < 2:
            continue
        question, answer = parts[0].strip().lower(), parts[1].strip()
        if not answer or "name" not in question:
            continue
        if any(word in question for word in _NOT_THE_KID):
            continue
        first, _, last = " ".join(answer.split()).partition(" ")
        return first, (last or None)
    return None, None


# Which ms_bookings column holds the slot's time of day isn't pinned down
# across Sytist versions, so the booking query reads every column and the
# first of these that's present is used.
_TIME_COLUMNS = ("book_time", "book_start_time", "book_start", "book_time_start")
_TIME_FORMATS = ("%H:%M:%S", "%H:%M", "%I:%M %p", "%I:%M%p", "%I:%M:%S %p")


def _clock(value) -> str | None:
    """A time-of-day value as "HH:MM", or None."""
    if value is None or value == "":
        return None
    if isinstance(value, timedelta):            # pymysql returns TIME as timedelta
        minutes = int(value.total_seconds()) // 60
        return f"{minutes // 60 % 24:02d}:{minutes % 60:02d}"
    if isinstance(value, (time, datetime)):
        return value.strftime("%H:%M")
    if isinstance(value, (int, float)) and 0 <= value < 24 * 60:
        return f"{int(value) // 60:02d}:{int(value) % 60:02d}"  # minutes after midnight
    text = str(value).strip().upper()
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%H:%M")
        except ValueError:
            continue
    return None


def booked_at(row: dict) -> str | None:
    """When the booking's slot is, as "YYYY-MM-DD HH:MM" (or just the date
    when no time is stored), so it sorts as text. None without a date."""
    day = row.get("book_date")
    if not day:
        return None
    day = day.strftime("%Y-%m-%d") if isinstance(day, (date, datetime)) else str(day)[:10]
    clock = next((c for c in (_clock(row.get(k)) for k in _TIME_COLUMNS) if c), None)
    return f"{day} {clock}" if clock else day


class SytistSource:
    """The queries Player Sort runs. Tests swap in a fake with the same
    methods (see sytist_sync.get_source)."""

    def __init__(self, db: DbSession):
        self.db = db

    def ping(self) -> dict:
        with connect(self.db) as conn:
            _query(conn, "SELECT 1 AS ok")
        return {"ok": True}

    def booking_events(self, limit: int = 100, keep_days: int = 3) -> list[dict]:
        """Booking-calendar sign-ups grouped the way a shoot is picked:
        special events (book_special_event_id > 0, one row per event, which
        can span several dates) and regular calendar days (no event, one row
        per date). Only events whose last booking is today, upcoming, or at
        most `keep_days` days ago are listed, soonest first."""
        with connect(self.db) as conn:
            events = _query(conn, """
                SELECT b.book_special_event_id AS id, c.date_title AS page_title,
                       sd.sd_title AS session_title,
                       MIN(b.book_date) AS first_date, MAX(b.book_date) AS last_date,
                       COUNT(*) AS bookings
                FROM ms_bookings b
                LEFT JOIN ms_booking_special_dates sd
                       ON sd.sd_id = b.book_special_event_id
                LEFT JOIN ms_calendar c ON c.date_id = sd.sd_date_id
                WHERE b.book_special_event_id > 0
                GROUP BY b.book_special_event_id, c.date_title, sd.sd_title
                HAVING MAX(b.book_date) >= CURDATE() - INTERVAL %s DAY
                ORDER BY MIN(b.book_date) ASC
                LIMIT %s
            """, (int(keep_days), int(limit)))
            days = _query(conn, """
                SELECT b.book_date AS day, COUNT(*) AS bookings
                FROM ms_bookings b
                WHERE (b.book_special_event_id = 0 OR b.book_special_event_id IS NULL)
                  AND b.book_date >= CURDATE() - INTERVAL %s DAY
                GROUP BY b.book_date
                ORDER BY b.book_date ASC
                LIMIT %s
            """, (int(keep_days), int(limit)))
        out = [{
            "kind": "event", "id": int(r["id"]),
            "title": _event_title(r),
            "first_date": str(r["first_date"] or ""), "last_date": str(r["last_date"] or ""),
            "bookings": int(r["bookings"] or 0),
        } for r in events]
        out += [{
            "kind": "day", "id": str(r["day"]), "title": "Booking calendar",
            "first_date": str(r["day"]), "last_date": str(r["day"]),
            "bookings": int(r["bookings"] or 0),
        } for r in days if r["day"]]
        out.sort(key=lambda e: (e["first_date"], e["title"]))
        return out[:limit]

    def galleries(self, q: str = "", limit: int = 50) -> list[dict]:
        sql = """
            SELECT cal.date_id AS id, cal.date_title AS title,
                   (SELECT COUNT(*) FROM ms_pre_register r
                    WHERE r.reg_date_id = cal.date_id
                      AND (r.reg_group_value IS NULL OR r.reg_group_value = ''))
                   AS registrations
            FROM ms_calendar cal
        """
        params: list = []
        if q.strip():
            sql += " WHERE cal.date_title LIKE %s"
            params.append(f"%{q.strip()}%")
        sql += " ORDER BY cal.date_id DESC LIMIT %s"
        params.append(int(limit))
        with connect(self.db) as conn:
            rows = _query(conn, sql, tuple(params))
        return [{"id": r["id"], "title": r["title"],
                 "registrations": int(r["registrations"] or 0)} for r in rows]

    def families(self, booking_event_ids, gallery_ids, booking_dates=()) -> list[dict]:
        out: list[dict] = []
        booking_event_ids = [int(x) for x in booking_event_ids or []]
        gallery_ids = [int(x) for x in gallery_ids or []]
        booking_dates = [str(x) for x in booking_dates or []]
        where, params = [], []
        if booking_event_ids:
            where.append(f"book_special_event_id IN ({_placeholders(booking_event_ids)})")
            params += booking_event_ids
        if booking_dates:
            where.append(
                "((book_special_event_id = 0 OR book_special_event_id IS NULL)"
                f" AND book_date IN ({_placeholders(booking_dates)}))")
            params += booking_dates
        with connect(self.db) as conn:
            if where:
                for r in _query(conn, f"""
                    SELECT *
                    FROM ms_bookings
                    WHERE {" OR ".join(where)}
                """, tuple(params)):
                    first, last = r["book_subject_first_name"], r["book_subject_last_name"]
                    if not (first or "").strip() and not (last or "").strip():
                        first, last = subject_from_options(r.get("book_options"))
                    out.append({
                        "source": "booking", "source_id": str(r["book_id"]),
                        "subject_first_name": first,
                        "subject_last_name": last,
                        "parent_first_name": r["book_first_name"],
                        "parent_last_name": r["book_last_name"],
                        "parent_email": r["book_email"],
                        "parent_phone": r["book_phone"],
                        "booked_at": booked_at(r),
                    })
            if gallery_ids:
                for r in _query(conn, f"""
                    SELECT reg_id, reg_subject_first_name, reg_subject_last_name,
                           reg_first_name, reg_last_name, reg_email, reg_phone
                    FROM ms_pre_register
                    WHERE reg_date_id IN ({_placeholders(gallery_ids)})
                      AND (reg_group_value IS NULL OR reg_group_value = '')
                """, tuple(gallery_ids)):
                    out.append({
                        "source": "preregister", "source_id": str(r["reg_id"]),
                        "subject_first_name": r["reg_subject_first_name"],
                        "subject_last_name": r["reg_subject_last_name"],
                        "parent_first_name": r["reg_first_name"],
                        "parent_last_name": r["reg_last_name"],
                        "parent_email": r["reg_email"],
                        "parent_phone": r["reg_phone"],
                    })
        return out
