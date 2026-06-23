"""'This Week in History' rollover timing — PURE, TZ-aware. The app is stateless / external-scheduler,
so there's no resident timer: these primitives let a run decide, from ``now`` (already in the household
TZ) + the persisted ``last_rollover``, whether a week boundary was crossed (→ purge + rotate) and whether
it's inside the Saturday-evening PRE-ROLL window (→ reclaim-first + pre-fetch next week).

The Sun–Sat week is identified by its SUNDAY's date (isoformat) — an unambiguous unique id (no ISO-week-
vs-Sunday-start mismatch). Boundary instants are computed with date/clock arithmetic that PRESERVES the
TZ, so a DST-transition Saturday still resolves to a well-defined Sunday 00:00. No I/O."""
from __future__ import annotations

from datetime import datetime, timedelta

from scripts.managers.machine_learning.discovery.window import week_window


def week_stamp(now) -> str:
    """The current Sun–Sat week's unique id — its Sunday's date, isoformat (e.g. ``2024-12-29``)."""
    return week_window(now)[0].isoformat()


def rollover_due(now, last_rollover) -> bool:
    """True iff a rollover (purge + rotate) is owed: a prior stamp exists AND the current week differs
    from it. First run (no ``last_rollover``) → False (nothing to roll over). A multi-week gap still
    returns True once — the stale trials are purged regardless of how many weeks were missed."""
    if not last_rollover:
        return False
    return week_stamp(now) != str(last_rollover)


def next_boundary(now) -> datetime:
    """The next Sun 00:00 STRICTLY after ``now`` — the instant the current Sun–Sat week ends / the next
    begins. TZ is preserved (so the boundary is Sunday 00:00 in the household zone)."""
    if not isinstance(now, datetime):
        now = datetime(now.year, now.month, now.day)
    days_ahead = (6 - now.weekday()) % 7            # weekday(): Mon=0…Sun=6 → 0 when today is Sunday
    sunday = (now + timedelta(days=days_ahead)).replace(hour=0, minute=0, second=0, microsecond=0)
    if sunday <= now:                               # today is Sunday but past 00:00 → the NEXT one
        sunday += timedelta(days=7)
    return sunday


def in_pre_roll(now, lead_hours: float = 8) -> bool:
    """True iff ``now`` is inside the pre-roll window ``[next_boundary − lead_hours, next_boundary)`` —
    the Saturday-evening lead-in where next week is computed + pre-fetched. Needs a time-of-day, so a
    bare ``date`` (no clock) returns False."""
    if not isinstance(now, datetime):
        return False
    boundary = next_boundary(now)
    start = boundary - timedelta(hours=max(0.0, float(lead_hours)))
    return start <= now < boundary
