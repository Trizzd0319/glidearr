"""Tests for the 'This Week in History' watchability floor — input mapping, fail-closed exclusion, and
watchability-descending ranking. The scorer is a fake (the real one is exercised in acquisition tests)."""
from __future__ import annotations

from scripts.managers.machine_learning.discovery.scoring import score_and_floor, to_scorer_input


class _Scorer:
    """Returns the candidate's preset total; a ``None`` preset / raise models an unscoreable candidate."""
    def __init__(self, by_tmdb):
        self.by_tmdb = by_tmdb

    def score(self, cand):
        total = self.by_tmdb.get(cand["ids"].get("tmdb"))
        if total == "boom":
            raise RuntimeError("unscoreable")
        return {"total": total, "matrix": {"popularity": total}, "evidence": {}}

    def reason(self, matrix, *, evidence=None):
        return f"popularity={matrix.get('popularity')}"


def test_to_scorer_input_maps_media_and_ids():
    movie = to_scorer_input({"media": "movie", "tmdb_id": 5, "genres": ["Action"], "votes": 100, "year": 1999})
    assert movie["type"] == "movie" and movie["ids"] == {"tmdb": 5} and movie["votes"] == 100
    show = to_scorer_input({"media": "show", "tvdb_id": 9})
    assert show["type"] == "show" and show["ids"] == {"tvdb": 9} and show["source"] == "discovery"


def test_floor_is_fail_closed_and_ranks_descending():
    cands = [
        {"media": "movie", "tmdb_id": 1},      # 90 → kept (top)
        {"media": "movie", "tmdb_id": 2},      # 40 → below floor 50 → dropped
        {"media": "movie", "tmdb_id": 3},      # 70 → kept
        {"media": "movie", "tmdb_id": 4},      # None total → fail-closed drop
        {"media": "movie", "tmdb_id": 5},      # scorer raises → fail-closed drop
    ]
    scorer = _Scorer({1: 90, 2: 40, 3: 70, 4: None, 5: "boom"})
    out = score_and_floor(cands, scorer, floor=50)
    assert [c["tmdb_id"] for c in out] == [1, 3]            # 40/None/raise all excluded
    assert [c["score"] for c in out] == [90, 70]           # watchability-descending
    assert out[0]["why"] == "popularity=90"                # reason() threaded through


def test_floor_zero_keeps_all_scoreable():
    cands = [{"media": "movie", "tmdb_id": 1}, {"media": "movie", "tmdb_id": 2}]
    out = score_and_floor(cands, _Scorer({1: 5, 2: 0}), floor=0)
    assert {c["tmdb_id"] for c in out} == {1, 2}            # 0 >= 0 stays; only None/raise drop
