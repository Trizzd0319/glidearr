"""Tests for the OPTIONAL caught-up recency BOOST (default-off) — recency_value()
in timeline.py and the recency_boost group-rank tier in ordering.order_items.

The locked design: watchability stays the PRIMARY group ranking and groups stay
contiguous; the boost only lifts a GROUP that the user is CAUGHT UP on AND whose
freshest item is RECENT, and only when ``recency_boost`` is explicitly on (so the
default output is byte-identical to today)."""
from __future__ import annotations

from datetime import date
from itertools import permutations

from scripts.managers.machine_learning.playlists.models import PlaylistInput
from scripts.managers.machine_learning.playlists.ordering import order_items
from scripts.managers.machine_learning.playlists.timeline import recency_value

# a fixed "today" so window math is deterministic regardless of the real clock.
NOW = date(2026, 6, 18)


def mv(rk, score=None, **kw):
    return PlaylistInput(rating_key=rk, medium="movie", score=score, **kw)


def ep(rk, s, e, score=None, **kw):
    return PlaylistInput(rating_key=rk, medium="episode", series_id=kw.pop("sid", 1),
                         season=s, episode=e, score=score, **kw)


def _rks(plan):
    return [i.rating_key for i in plan.items]


def _plan_tuple(plan):
    """A fully comparable snapshot of a plan (for byte-identity assertions)."""
    return (plan.family, plan.considered, plan.dropped_watched, plan.truncated,
            tuple(plan.coverage.items()),
            tuple((i.rating_key, i.ordinal, i.group_key, i.group_kind, i.score, i.reason)
                  for i in plan.items))


# ── (1) golden: recency_boost=False is byte-identical to today's output ────────
def test_boost_off_is_byte_identical_to_default():
    # a mixed fixture (universe group + standalones + a series), some with the NEW
    # added_at field populated. With the boost OFF (the default), added_at must be
    # completely inert — the plan must equal the plan built without ever passing it.
    items = [mv("u1", 70, universes=("mcu",), release_date="2008-01-01", added_at="2026-06-01"),
             mv("u2", 80, universes=("mcu",), release_date="2010-01-01"),
             mv("hi", 90, added_at="2026-06-17"),
             mv("lo", 40),
             ep("e1", 1, 1, score=55, sid=3, air_date="2026-06-10", added_at="2026-06-10"),
             ep("e2", 1, 2, score=55, sid=3, air_date="2026-06-11")]
    default = order_items(items)
    explicit_off = order_items(items, recency_boost=False, now=NOW)
    # explicit default-off path equals the default call (signature default is False)
    assert _plan_tuple(default) == _plan_tuple(explicit_off)
    # and equals the SAME items with every added_at stripped (added_at is inert when off)
    stripped = [PlaylistInput(**{**it.__dict__, "added_at": None}) for it in items]
    assert _plan_tuple(order_items(stripped)) == _plan_tuple(default)


# ── recency_value: the DESC-safe mirror of chrono_value ────────────────────────
def test_recency_value_sinks_undated_to_neg_inf():
    # chrono_value would float an undated item to +inf (top under DESC) — recency_value
    # must sink it to -inf so it never out-ranks a dated item under a recency sort.
    assert recency_value(mv("x"), now=NOW) == float("-inf")           # no date at all
    assert recency_value(mv("y", year=2020), now=NOW) == float("-inf")  # year is NOT a fallback


def test_recency_value_blends_added_at_and_air_date_taking_the_max():
    # a long-owned but newly-aired episode reads as fresh via air_date; a freshly
    # ACQUIRED old film reads as fresh via added_at. Blend = max(both).
    fresh_air = ep("a", 1, 1, added_at="2010-01-01", air_date="2026-06-10")
    fresh_add = mv("b", added_at="2026-06-15", release_date="1999-01-01")
    assert recency_value(fresh_air, now=NOW) == float(date(2026, 6, 10).toordinal())
    assert recency_value(fresh_add, now=NOW) == float(date(2026, 6, 15).toordinal())


def test_recency_value_clamps_future_dates_to_now():
    # an unaired, future-stamped episode must not win — clamp to now.
    future = ep("f", 9, 9, air_date="2099-01-01")
    assert recency_value(future, now=NOW) == float(NOW.toordinal())


def test_recency_value_ignores_impossible_calendar_dates():
    assert recency_value(mv("bad", release_date="2026-13-40"), now=NOW) == float("-inf")


# ── (3) golden: an undated item never out-ranks a dated one under the boost ────
def test_undated_item_never_outranks_dated_under_recency():
    # two standalone movies, equal watchability; one dated-recent, one undated. With
    # the boost on, the dated-recent one qualifies and the undated one cannot.
    dated = mv("dated", 50, added_at="2026-06-10")
    undated = mv("undated", 50)
    plan = order_items([undated, dated], recency_boost=True, now=NOW)
    assert _rks(plan)[0] == "dated"
    # and recency_value confirms the relation directly
    assert recency_value(undated, now=NOW) < recency_value(dated, now=NOW)


# ── (2) golden: a caught-up fresh group floats above a stale higher-watchability
#        group ONLY when the boost is on ──────────────────────────────────────--
def _two_group_fixture():
    # group A: a series the user is CAUGHT UP on (watched S1E1, unwatched S1E2 is the
    # freshest + recent) but only MODEST watchability (40).
    a_watched = ep("a1", 1, 1, score=40, sid=1, watched=True, air_date="2026-05-01")
    a_live = ep("a2", 1, 2, score=40, sid=1, air_date="2026-06-12")
    # group B: a standalone with HIGHER watchability (90) but STALE (old, nothing fresh).
    b = mv("b", 90, release_date="2005-01-01", added_at="2005-01-01")
    return [a_watched, a_live, b]


def test_caught_up_fresh_group_floats_above_stale_higher_watchability_only_when_on():
    items = _two_group_fixture()
    off = order_items(items, now=NOW)
    on = order_items(items, recency_boost=True, now=NOW)
    # OFF: watchability rules → the 90 standalone leads, the modest series trails.
    assert _rks(off) == ["b", "a2"]
    # ON: the caught-up + fresh series is boosted ABOVE the stale higher-watch group.
    assert _rks(on) == ["a2", "b"]


# ── (4) golden: a NOT-caught-up group (older unwatched present) is NOT boosted ──
def test_not_caught_up_group_is_not_boosted():
    # the user has a RECENT watched episode (S1E2) but an OLDER unwatched one (S1E1) —
    # they are NOT caught up (an unwatched item is older than a watched one), so even
    # though the group has a recent item, it must NOT be boosted past the 90 standalone.
    a_live_old = ep("a1", 1, 1, score=40, sid=1, air_date="2026-05-01")
    a_watched_new = ep("a2", 1, 2, score=40, sid=1, watched=True, air_date="2026-06-12")
    b = mv("b", 90, release_date="2005-01-01")
    items = [a_live_old, a_watched_new, b]
    plan = order_items(items, recency_boost=True, now=NOW)
    assert _rks(plan) == ["b", "a1"]      # NOT boosted: watchability order holds


def test_standalone_with_no_prior_history_qualifies_on_freshness_alone():
    # nothing watched before it → trivially caught up; recent → boosted over a stale
    # higher-watchability group.
    fresh = mv("fresh", 30, added_at="2026-06-15")
    stale_hi = mv("stale", 95, release_date="2001-01-01")
    plan = order_items([fresh, stale_hi], recency_boost=True, now=NOW)
    assert _rks(plan) == ["fresh", "stale"]


def test_fresh_caught_up_but_outside_window_is_not_boosted():
    # caught up + would-be fresh, but the freshest item is older than window_days → no boost.
    a_live = mv("a", 30, added_at="2026-05-01")     # ~48 days before NOW
    b = mv("b", 90, release_date="2001-01-01")
    plan = order_items([a_live, b], recency_boost=True, window_days=30, now=NOW)
    assert _rks(plan) == ["b", "a"]                 # outside 30-day window → unboosted
    # widen the window and it qualifies
    wide = order_items([a_live, b], recency_boost=True, window_days=120, now=NOW)
    assert _rks(wide) == ["a", "b"]


def test_boost_never_breaks_group_contiguity_or_spoiler_order():
    # a boosted multi-item series stays contiguous and in (season, episode) order.
    items = [ep("e2", 1, 2, score=40, sid=7, air_date="2026-06-12"),
             ep("e1", 1, 1, score=40, sid=7, air_date="2026-06-05"),
             mv("hi", 95, release_date="2001-01-01")]
    plan = order_items(items, recency_boost=True, now=NOW)
    assert _rks(plan) == ["e1", "e2", "hi"]         # group lifted, still e1 before e2
