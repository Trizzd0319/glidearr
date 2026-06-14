"""Tests for the pure MAL-upcoming watchability gate in the calendar manager.

mal_upcoming_above_threshold scores each MAL seasonal entry with the brain's
score_show on the signals an unowned upcoming anime actually has (genres × the
household genre affinity + the MAL community mean as the rating) and keeps the
entries at/above the threshold, highest first.
"""
from __future__ import annotations

from scripts.managers.services.calendar import mal_upcoming_above_threshold

_AFF = {"genres": {"Action": 30, "Fantasy": 12}}


def _entry(title, mean=None, genres=(), media_type="tv", mal_id=1, en=None):
    node = {
        "id": mal_id, "title": title, "media_type": media_type,
        "genres": [{"name": g} for g in genres],
        "start_season": {"year": 2026, "season": "spring"},
    }
    if mean is not None:
        node["mean"] = mean
    if en:
        node["alternative_titles"] = {"en": en}
    return {"node": node}


def test_threshold_separates_acclaimed_from_junk():
    seasonal = [
        _entry("Acclaimed Hit", mean=8.6, genres=("Action", "Fantasy"), mal_id=1),  # ≈25
        _entry("Mediocre", mean=6.6, genres=("Action",), mal_id=2),                  # ≈14
        _entry("Unrated Nobody", mean=None, genres=("Romance",), mal_id=3),          # ≈2
        _entry("Panned", mean=3.0, genres=("Action",), mal_id=4),                    # ≈1
    ]
    out = mal_upcoming_above_threshold(seasonal, genre_affinity=_AFF, threshold=20)
    assert [e["mal_id"] for e in out] == [1]
    assert out[0]["watchability"] >= 20 and out[0]["mean"] == 8.6


def test_sorted_highest_first_and_threshold_inclusive():
    seasonal = [
        _entry("B", mean=8.6, genres=("Romance",), mal_id=2),                 # ≈22 (no affinity)
        _entry("A", mean=8.6, genres=("Action", "Fantasy"), mal_id=1),        # ≈25 (affinity)
    ]
    out = mal_upcoming_above_threshold(seasonal, genre_affinity=_AFF, threshold=22)
    assert [e["mal_id"] for e in out] == [1, 2]            # highest watchability first
    assert out[1]["watchability"] >= 22                     # >= is inclusive


def test_malformed_and_titleless_entries_drop():
    seasonal = [
        None, "junk", {"node": "junk"}, {"node": {}},               # malformed
        {"node": {"id": 9, "mean": 9.9}},                            # no title
        _entry("Bad Mean", mean="not-a-number", genres=("Action",), mal_id=5),  # mean coerces None
        _entry("Real", mean=9.0, genres=("Action",), mal_id=6),
    ]
    out = mal_upcoming_above_threshold(seasonal, genre_affinity=_AFF, threshold=20)
    assert [e["mal_id"] for e in out] == [6]


def test_empty_and_no_affinity_are_safe():
    assert mal_upcoming_above_threshold([]) == []
    assert mal_upcoming_above_threshold(None) == []
    # no affinity at all — pure critic gate still works
    out = mal_upcoming_above_threshold(
        [_entry("Hit", mean=8.6, genres=("Drama",), mal_id=1)], threshold=20)
    assert [e["mal_id"] for e in out] == [1]


def test_movie_entries_carry_media_type_and_alt_title():
    out = mal_upcoming_above_threshold(
        [_entry("Gekijouban Z", mean=8.8, genres=("Action",), media_type="movie",
                mal_id=7, en="Z: The Movie")],
        genre_affinity=_AFF, threshold=20)
    assert out and out[0]["media_type"] == "movie" and out[0]["title_en"] == "Z: The Movie"
