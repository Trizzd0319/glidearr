"""Tests for the pure next-watch propensity layer (DESIGN §5.1)."""
from __future__ import annotations

from scripts.managers.machine_learning.next_watch import rank_next_watch, watchlist_intent


def _u(title, tmdb, by):
    return {"title": title, "type": "movie", "ids": {"tmdb": tmdb}, "watchlisted_by": by}


def test_intent_rises_with_member_count_and_caps():
    union = [_u("Solo", 1, ["a"]), _u("Duo", 2, ["a", "b"]), _u("Many", 3, list("abcdefg"))]
    out = watchlist_intent(union)
    assert out["1"]["intent"] == 60.0
    assert out["2"]["intent"] == 72.0          # 60 + 12
    assert out["3"]["intent"] == 100.0         # capped


def test_intent_dedups_keeping_strongest():
    union = [_u("X", 9, ["a"]), _u("X", 9, ["a", "b", "c"])]
    out = watchlist_intent(union)
    assert out["9"]["intent"] == 84.0          # 60 + 24 (the stronger of the two)


def test_intent_skips_items_without_id():
    assert watchlist_intent([{"title": None, "ids": {}, "watchlisted_by": []}]) == {}


def test_rank_owned_first_then_intent():
    union = [_u("OwnedWeak", 1, ["a"]), _u("UnownedStrong", 2, ["a", "b", "c"])]
    rows = rank_next_watch(union, owned_ids={"1"})
    assert rows[0]["primary_id"] == "1" and rows[0]["owned"]    # owned + unwatched tops next-watch
    assert rows[1]["primary_id"] == "2" and not rows[1]["owned"]
