"""Tests for playlists/timeline — spoiler-safe within-group ordering."""
from __future__ import annotations

from scripts.managers.machine_learning.playlists.models import PlaylistInput
from scripts.managers.machine_learning.playlists.spoiler import is_spoiler_safe
from scripts.managers.machine_learning.playlists.timeline import chrono_value, order_within_group


def ep(rk, s, e, **kw):
    return PlaylistInput(rating_key=rk, medium="episode", series_id=kw.pop("sid", 1),
                         season=s, episode=e, **kw)


def mv(rk, **kw):
    return PlaylistInput(rating_key=rk, medium="movie", **kw)


def _order(members):
    return [m.rating_key for m in order_within_group(members)]


# ── the red-team CRITICAL: (season,episode) is authoritative, NOT air date ──────
def test_episodes_order_by_se_even_with_null_or_reversed_air_dates():
    # E1 has NO air date, E2/E3 have dates that DISAGREE with sequence. Date-sorting
    # would put dateless E1 last (spoiler); (season,episode) keeps E1→E2→E3.
    eps = [ep("E2", 1, 2, air_date="2020-01-01"),
           ep("E1", 1, 1, air_date=None),
           ep("E3", 1, 3, air_date="2019-06-01")]
    assert _order(eps) == ["E1", "E2", "E3"]
    assert is_spoiler_safe(order_within_group(eps))


def test_specials_sink_to_track_tail():
    eps = [ep("S", 0, 1, is_special=True), ep("E1", 1, 1), ep("E2", 1, 2)]
    assert _order(eps) == ["E1", "E2", "S"]


def test_movies_in_group_order_by_release_then_year_fallback():
    ms = [mv("late", release_date="2012-05-04"),
          mv("early", release_date="2008-05-02"),
          mv("noDate", year=2010)]                 # year fallback lands between
    assert _order(ms) == ["early", "noDate", "late"]


def test_cross_media_interleave_by_lead_date_series_stays_atomic():
    # franchise: film(2008) + a show whose episodes aired 2013 → film leads, then the
    # show as a contiguous block (never split across the film).
    members = [ep("e2", 1, 2, sid=9, air_date="2013-09-29"),
               ep("e1", 1, 1, sid=9, air_date="2013-09-22"),
               mv("film", release_date="2008-05-02")]
    assert _order(members) == ["film", "e1", "e2"]


def test_explicit_timeline_index_overrides_dates():
    members = [mv("b", release_date="2001-01-01", timeline_index=2),
              mv("a", release_date="2020-01-01", timeline_index=1)]
    assert _order(members) == ["a", "b"]          # curated order wins over release date


def test_two_series_in_a_group_interleave_by_lead_date_each_atomic():
    members = [ep("b1", 1, 1, sid=2, air_date="2015-01-01"),
               ep("a1", 1, 1, sid=1, air_date="2010-01-01"),
               ep("a2", 1, 2, sid=1, air_date="2010-02-01"),
               ep("b2", 1, 2, sid=2, air_date="2015-02-01")]
    # series 1 (lead 2010) before series 2 (lead 2015); each contiguous + in s/e order
    assert _order(members) == ["a1", "a2", "b1", "b2"]


def test_order_is_deterministic_regardless_of_input_order():
    base = [ep("e1", 1, 1), ep("e2", 1, 2), mv("m", release_date="2000-01-01")]
    from itertools import permutations
    outs = {tuple(_order(list(p))) for p in permutations(base)}
    assert len(outs) == 1                          # input-order independent


def test_chrono_value_sentinels():
    assert chrono_value(mv("x")) == float("inf")           # no date, no year → tail
    assert chrono_value(mv("y", year=1999)) == 19990000.0
    assert chrono_value(ep("z", 1, 1, air_date="2021-03-04")) == 20210304.0
