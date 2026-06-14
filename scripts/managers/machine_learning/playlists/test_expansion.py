"""Tests for playlists/expansion — show → capped episode set."""
from __future__ import annotations

from scripts.managers.machine_learning.playlists.expansion import (
    FULL_SERIES,
    NEXT_UNWATCHED,
    expand_show,
)
from scripts.managers.machine_learning.playlists.models import PlaylistInput


def ep(rk, s, e, watched=False, **kw):
    return PlaylistInput(rating_key=rk, medium="episode", series_id=1, season=s,
                         episode=e, watched=watched, **kw)


def _rks(items):
    return [i.rating_key for i in items]


def test_next_unwatched_skips_watched_and_caps():
    eps = [ep("e1", 1, 1, watched=True), ep("e2", 1, 2, watched=True),
           ep("e3", 1, 3), ep("e4", 1, 4), ep("e5", 1, 5)]
    assert _rks(expand_show(eps, mode=NEXT_UNWATCHED, cap=2)) == ["e3", "e4"]


def test_next_unwatched_orders_by_se_regardless_of_input():
    eps = [ep("e3", 1, 3), ep("e1", 1, 1), ep("e2", 1, 2)]
    assert _rks(expand_show(eps, mode=NEXT_UNWATCHED, cap=10)) == ["e1", "e2", "e3"]


def test_full_series_includes_watched_but_caps():
    eps = [ep("e1", 1, 1, watched=True), ep("e2", 1, 2), ep("e3", 1, 3)]
    assert _rks(expand_show(eps, mode=FULL_SERIES, cap=2)) == ["e1", "e2"]


def test_specials_excluded_by_default():
    eps = [ep("s", 0, 1, is_special=True), ep("e1", 1, 1)]
    assert _rks(expand_show(eps)) == ["e1"]
    assert set(_rks(expand_show(eps, include_specials=True))) == {"s", "e1"}


def test_cap_zero_returns_nothing():
    assert expand_show([ep("e1", 1, 1)], cap=0) == []
