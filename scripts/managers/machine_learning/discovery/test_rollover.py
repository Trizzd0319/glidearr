"""Tests for the rollover timing primitives — week stamping, boundary detection, the pre-roll window,
and the idempotent due-check (incl. first-run, week-advance, multi-week gap, TZ preservation)."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from scripts.managers.machine_learning.discovery.rollover import (
    in_pre_roll,
    next_boundary,
    rollover_due,
    week_stamp,
)


def test_week_stamp_is_the_weeks_sunday():
    assert week_stamp(datetime(2024, 12, 31)) == "2024-12-29"   # Tue → Sun-of-week 2024-12-29
    assert week_stamp(datetime(2025, 1, 4)) == "2024-12-29"     # Sat, same Sun–Sat week
    assert week_stamp(datetime(2025, 1, 5)) == "2025-01-05"     # Sun → new week starts


def test_rollover_due_first_run_advance_and_gap():
    assert rollover_due(datetime(2024, 12, 31), None) is False         # first run → nothing to roll
    assert rollover_due(datetime(2024, 12, 31), "2024-12-29") is False  # same week
    assert rollover_due(datetime(2025, 1, 5), "2024-12-29") is True     # week advanced
    assert rollover_due(datetime(2025, 2, 2), "2024-12-29") is True     # multi-week gap → still due once


def test_next_boundary_is_the_upcoming_sunday_midnight():
    assert next_boundary(datetime(2024, 12, 25, 14, 0)) == datetime(2024, 12, 29, 0, 0)  # Wed → coming Sun
    assert next_boundary(datetime(2024, 12, 28, 22, 0)) == datetime(2024, 12, 29, 0, 0)  # Sat eve → next day
    # Sunday past midnight → the boundary ENDING the current week is next Sunday, not today
    assert next_boundary(datetime(2024, 12, 29, 12, 0)) == datetime(2025, 1, 5, 0, 0)


def test_pre_roll_window():
    # Sat 20:00 with an 8h lead → window [Sat 16:00, Sun 00:00) → inside.
    assert in_pre_roll(datetime(2024, 12, 28, 20, 0), lead_hours=8) is True
    assert in_pre_roll(datetime(2024, 12, 28, 10, 0), lead_hours=8) is False   # before the window
    assert in_pre_roll(datetime(2024, 12, 25, 23, 0), lead_hours=8) is False   # mid-week
    assert in_pre_roll(datetime(2024, 12, 28).date(), lead_hours=8) is False   # bare date → no clock


def test_boundary_preserves_timezone():
    tz = ZoneInfo("America/New_York")
    now = datetime(2024, 12, 28, 20, 0, tzinfo=tz)             # Sat eve, Eastern
    b = next_boundary(now)
    assert b == datetime(2024, 12, 29, 0, 0, tzinfo=tz) and b.tzinfo is tz
    assert in_pre_roll(now, lead_hours=8) is True


def test_dst_transition_saturday_resolves_to_sunday_midnight():
    # US spring-forward is Sun 2024-03-10; the Saturday before still resolves to Sun 00:00 local.
    tz = ZoneInfo("America/New_York")
    now = datetime(2024, 3, 9, 21, 0, tzinfo=tz)
    assert next_boundary(now) == datetime(2024, 3, 10, 0, 0, tzinfo=tz)
    assert in_pre_roll(now, lead_hours=8) is True
