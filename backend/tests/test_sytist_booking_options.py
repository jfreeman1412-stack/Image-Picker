"""Booking forms put the kid's name in a custom question (book_options)
rather than book_subject_first/last_name (2026-10-09)."""
from app.services.sytist_db import subject_from_options


def test_gymnast_name_question():
    opts = ("Gymnast's Name:|Ava Smith|0.00\n"
            "Gymnast's Level:|Kids 1 Gymnastics|0.00\n"
            "Parent/Guardian Email Address:|jane@example.com|0.00")
    assert subject_from_options(opts) == ("Ava", "Smith")


def test_other_name_questions():
    assert subject_from_options("Skaters Name:|Mia Lee|0.00") == ("Mia", "Lee")
    assert subject_from_options("Player Name|Leo  Van Dyke|0.00") == ("Leo", "Van Dyke")
    assert subject_from_options("Athlete Name|Sam|0.00") == ("Sam", None)


def test_skips_parent_coach_and_team_names():
    opts = ("Coach Name|Bob Jones|0.00\n"
            "Team Name-Level|Tigers 10U|0.00\n"
            "Parent/Guardian First Name|Jane|0.00\n"
            "Rider's Name:|Kai Moss|0.00")
    assert subject_from_options(opts) == ("Kai", "Moss")


def test_no_name_answer():
    assert subject_from_options("") == (None, None)
    assert subject_from_options(None) == (None, None)
    assert subject_from_options("Gymnast's Name:||0.00\nShipping Address|1 Main St|0") == (None, None)
    assert subject_from_options("Coach Name|Bob Jones|0.00") == (None, None)


def test_windows_line_endings():
    assert subject_from_options("Gymnasts Name:|Ivy Park|0.00\r\nSport|Gym|0") == ("Ivy", "Park")


def test_booked_at_formats():
    from datetime import date, timedelta
    from app.services.sytist_db import booked_at
    assert booked_at({"book_date": date(2026, 10, 10), "book_time": timedelta(hours=9, minutes=5)}) \
        == "2026-10-10 09:05"
    assert booked_at({"book_date": "2026-10-10", "book_time": "1:30 PM"}) == "2026-10-10 13:30"
    assert booked_at({"book_date": "2026-10-10", "book_start": "14:00:00"}) == "2026-10-10 14:00"
    assert booked_at({"book_date": "2026-10-10", "book_time": ""}) == "2026-10-10"
    assert booked_at({"book_date": None, "book_time": "9:00 AM"}) is None
