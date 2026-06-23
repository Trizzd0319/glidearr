"""Tests for the 'This Week in History' window engine — the bug-prone primitive (the doc's case list)."""
from __future__ import annotations

from datetime import date, datetime, timezone

from scripts.managers.machine_learning.discovery.window import (
    released_this_week,
    week_window,
    years_ago,
)


def test_week_window_is_seven_consecutive_days_starting_sunday():
    sunday, days, md = week_window(date(2023, 5, 30))            # → Sun–Sat May 28–Jun 3 2023
    assert sunday == date(2023, 5, 28) and days[0] == sunday and len(days) == 7
    assert days[6] == date(2023, 6, 3)
    assert md == {(5, 28), (5, 29), (5, 30), (5, 31), (6, 1), (6, 2), (6, 3)}


def test_month_boundary_wrap_matches_both_sides():
    now = date(2023, 5, 30)                                      # week May 28–Jun 3
    assert released_this_week(date(2015, 5, 31), now)            # late-May anniversary
    assert released_this_week(date(2015, 6, 1), now)             # early-June anniversary, same week
    assert not released_this_week(date(2015, 6, 5), now)         # outside the window


def test_year_boundary_wrap_surfaces_a_prior_year_january_title():
    now = date(2024, 12, 31)                                     # week Dec 29 2024 – Jan 4 2025
    _, _, md = week_window(now)
    assert (12, 31) in md and (1, 2) in md                       # straddles the year boundary
    assert released_this_week(date(2010, 1, 2), now)             # prior-year Jan-2 MUST surface (the doc case)
    assert released_this_week(date(2010, 12, 30), now)           # …and the December side too
    assert not released_this_week(date(2010, 6, 1), now)         # June not in this week


def test_this_year_future_anniversary_is_excluded():
    now = date(2025, 5, 30)
    assert not released_this_week(date(2025, 5, 31), now)        # anniversary is tomorrow this year → unreleased
    assert released_this_week(date(2010, 5, 31), now)            # same (month,day), a PAST year → released
    assert not released_this_week(date(2030, 1, 2), now)         # future title, never an anniversary yet


def test_feb29_non_leap_window_admits_a_feb29_title():
    now = date(2023, 2, 28)                                      # non-leap week Feb 26–Mar 4
    _, _, md = week_window(now)
    assert (2, 28) in md and (2, 29) in md                       # Feb-29 mapped onto the Feb-28 week
    assert released_this_week(date(2016, 2, 29), now)            # the leap-day title surfaces every year


def test_feb29_leap_window_matches_directly():
    now = date(2024, 2, 29)                                      # leap week Feb 25–Mar 2 includes a real Feb 29
    _, _, md = week_window(now)
    assert (2, 29) in md
    assert released_this_week(date(2016, 2, 29), now)


def test_tz_conversion_shifts_the_local_date():
    now = date(2024, 12, 31)                                     # week Dec 29 2024 – Jan 4 2025
    # 2010-01-02 02:00 UTC is still 2010-01-01 21:00 in US/Eastern → (month,day) = (1, 1), not (1, 2)
    rel = datetime(2010, 1, 2, 2, 0, tzinfo=timezone.utc)
    assert years_ago(rel, now, tz="America/New_York") and _md(rel, now, "America/New_York") == (1, 1)
    assert _md(rel, now, None) == (1, 2)                         # naive treatment keeps the UTC date


def test_none_and_unparseable_never_match():
    now = date(2024, 12, 31)
    assert not released_this_week(None, now)
    assert not released_this_week("not-a-date", now)
    assert not released_this_week("", now)
    assert years_ago(None, now) is None


def test_iso_string_dates_parse_like_datetimes():
    now = date(2024, 12, 31)                                     # week Dec 29 2024 – Jan 4 2025
    # Sonarr airDateUtc / Radarr release dates arrive as ISO strings, not date objects.
    assert released_this_week("2010-01-02T01:00:00Z", now)       # full UTC datetime string
    assert released_this_week("2010-01-02", now)                 # date-only string
    assert not released_this_week("2010-06-01T00:00:00Z", now)   # out of window
    assert years_ago("2010-01-02", now) == 15
    # tz conversion still applies to a tz-aware string: 02:00Z is Jan-1 in US/Eastern
    assert _md("2010-01-02T02:00:00Z", now, "America/New_York") == (1, 1)


def test_years_ago_uses_the_window_day_year():
    # Jan-2 title in a Dec→Jan week counts to the January side (2025), not `now`'s December year (2024)
    assert years_ago(date(2010, 1, 2), date(2024, 12, 31)) == 15
    assert years_ago(date(2015, 5, 31), date(2023, 5, 30)) == 8


def _md(release, now, tz):
    from scripts.managers.machine_learning.discovery.window import _as_date
    d = _as_date(release, tz)
    return (d.month, d.day)
