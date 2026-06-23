"""'This Week in History' window engine — the TZ-aware, year-agnostic anniversary-week primitive.

The current Sun–Sat calendar week of ``now`` is seven concrete dates in the household TZ, reduced to
seven ``(month, day)`` pairs. A title is an anniversary THIS week iff its REAL historical release/air
``(month, day)`` is one of those seven (in ANY past year) AND its real date is already ``<= now``
(this-year FUTURE anniversaries excluded). Year-agnostic SET membership makes month- and year-boundary
WRAP free (a Dec→Jan straddling week matches both ends with no range compares); Feb-29 anniversaries
map onto Feb-28 in non-leap windows so they surface every year.

PURE — no I/O. The CALLER converts ``air_date_utc`` to the household TZ and excludes season-0 specials
and sentinel/TBA dates (a widened Sonarr pull resurfaces placeholders that parse to a bogus Jan-1).

Policy notes (the doc calls this the most bug-prone primitive):
  * Build the seven dates with date arithmetic (DST-safe — no datetime shifting), in ONE household TZ.
  * The RELEASED check uses the title's REAL historical date ``<= now`` — NEVER
    ``release.replace(year=now.year)`` (which wrongly drops a Jan-2 title in a Dec→Jan straddling week).
  * Convert a tz-AWARE ``release`` to the household TZ BEFORE extracting ``(month, day)``.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


def _zone(tz):
    if tz is None or isinstance(tz, ZoneInfo):
        return tz
    try:
        return ZoneInfo(str(tz))
    except Exception:
        return None


def _as_date(value, tz=None):
    """A release/air value → a ``date`` in the household TZ. A tz-AWARE ``datetime`` is converted to
    ``tz`` first (so the ``(month, day)`` is the household-local one); a naive datetime / plain ``date``
    is taken as-is. ``None`` for anything undateable."""
    if isinstance(value, datetime):
        z = _zone(tz)
        if value.tzinfo is not None and z is not None:
            value = value.astimezone(z)
        return value.date()
    if isinstance(value, date):
        return value
    return None


def week_window(now):
    """``(sunday, [7 dates], {(month, day), …})`` for ``now``'s Sun–Sat week. ``now`` is a ``date`` /
    ``datetime`` ALREADY in the household TZ. The ``(month, day)`` set is year-agnostic; when the week
    includes Feb-28 it ALSO admits ``(2, 29)`` so a Feb-29 title surfaces in a non-leap year's window."""
    d = _as_date(now)
    sunday = d - timedelta(days=(d.weekday() + 1) % 7)            # weekday(): Mon=0…Sun=6 → days since Sunday
    days = [sunday + timedelta(days=i) for i in range(7)]
    md = {(x.month, x.day) for x in days}
    if (2, 28) in md:
        md.add((2, 29))                                          # non-leap weeks admit Feb-29 anniversaries
    return sunday, days, md


def released_this_week(release, now, *, tz=None) -> bool:
    """True iff ``release`` (a real historical release/air ``datetime`` / ``date``) is an ALREADY-RELEASED
    anniversary in ``now``'s Sun–Sat week. ``tz`` converts a tz-aware ``release`` to the household TZ
    before extracting ``(month, day)``; ``now`` must already be in that TZ. ``None`` / undateable → False;
    a real date AFTER ``now`` (this-year future anniversary / unreleased) → False."""
    rd = _as_date(release, tz)
    if rd is None:
        return False
    if rd > _as_date(now):                                       # not yet released → not an anniversary yet
        return False
    return (rd.month, rd.day) in week_window(now)[2]


def years_ago(release, now, *, tz=None):
    """Whole years from ``release`` to this week's anniversary (the "aired N years ago this week" hook).
    Uses the WINDOW DAY's year (so a Jan-2 title in a Dec→Jan week counts to the January side), not a raw
    year subtraction. ``None`` when undateable."""
    rd = _as_date(release, tz)
    if rd is None:
        return None
    _, days, _ = week_window(now)
    for d in days:
        if (d.month, d.day) == (rd.month, rd.day):
            return max(0, d.year - rd.year)
    if (rd.month, rd.day) == (2, 29):                            # Feb-29 mapped onto Feb-28 in a non-leap week
        for d in days:
            if (d.month, d.day) == (2, 28):
                return max(0, d.year - rd.year)
    return max(0, _as_date(now).year - rd.year)
