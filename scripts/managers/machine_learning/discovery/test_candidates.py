"""Tests for 'This Week in History' candidate generation — window filtering, ownership dedup/classify,
the season-0/null TV exclusions, series-level rollup, and the fail-closed popularity floor."""
from __future__ import annotations

from datetime import date, datetime

from scripts.managers.machine_learning.discovery.candidates import (
    episode_candidates,
    movie_candidates,
    partition_net_new,
)

_NOW = date(2024, 12, 31)                                         # week Dec 29 2024 – Jan 4 2025


def test_movie_candidates_filter_dedup_and_classify_ownership():
    rows = [
        {"tmdb_id": 1, "title": "In Window", "release_date": date(2010, 1, 2), "vote_count": 500},
        {"tmdb_id": 1, "title": "In Window (dup)", "release_date": date(2010, 1, 2), "vote_count": 500},
        {"tmdb_id": 2, "title": "Owned Dec", "release_date": date(2008, 12, 30), "vote_count": 900},
        {"tmdb_id": 3, "title": "Out of Window", "release_date": date(2010, 6, 1), "vote_count": 900},
    ]
    out = movie_candidates(rows, _NOW, owned_tmdbs={2})
    by_id = {c["tmdb_id"]: c for c in out}
    assert set(by_id) == {1, 2}                                  # tmdb 3 out of window; dup collapsed
    assert by_id[1]["owned"] is False and by_id[2]["owned"] is True
    assert by_id[1]["years_ago"] == 15 and by_id[1]["media"] == "movie"


def test_movie_in_cinemas_fallback_when_no_normalized_date():
    rows = [{"tmdb_id": 7, "title": "Theatrical", "in_cinemas_date": date(2000, 1, 1), "vote_count": 100}]
    out = movie_candidates(rows, _NOW)
    assert out and out[0]["tmdb_id"] == 7                        # (1,1) is in the Dec-29→Jan-4 window


def test_popularity_floor_is_fail_closed():
    rows = [
        {"tmdb_id": 1, "title": "Obscure", "release_date": date(2010, 1, 2), "vote_count": 5},
        {"tmdb_id": 2, "title": "No votes", "release_date": date(2010, 1, 2)},
        {"tmdb_id": 3, "title": "Popular", "release_date": date(2010, 1, 2), "vote_count": 500},
    ]
    out = movie_candidates(rows, _NOW, min_votes=50)
    assert [c["tmdb_id"] for c in out] == [3]                    # obscure + missing-data both excluded
    assert len(movie_candidates(rows, _NOW, min_votes=0)) == 3   # floor off → all three


def test_future_anniversary_excluded():
    rows = [{"tmdb_id": 1, "title": "Next Year", "release_date": date(2030, 1, 2), "vote_count": 500}]
    assert movie_candidates(rows, _NOW) == []                    # released_this_week guards real date <= now


def test_episode_candidates_rollup_to_series_keeping_oldest_anniversary():
    rows = [
        {"tvdb_id": 10, "series_title": "Show A", "season": 1, "episode": 1,
         "air_date_utc": datetime(2010, 1, 2)},                  # years_ago 15
        {"tvdb_id": 10, "series_title": "Show A", "season": 2, "episode": 5,
         "air_date_utc": datetime(2005, 1, 2)},                  # years_ago 20 → wins the rollup
        {"tvdb_id": 11, "series_title": "Specials", "season": 0, "episode": 1,
         "air_date_utc": datetime(2010, 1, 2)},                  # season-0 → excluded
        {"tvdb_id": 12, "series_title": "No Air", "season": 1, "episode": 1, "air_date_utc": None},
        {"tvdb_id": 13, "series_title": "Off Window", "season": 1, "episode": 1,
         "air_date_utc": datetime(2010, 6, 1)},                  # out of window
    ]
    out = episode_candidates(rows, _NOW)
    by_id = {c["tvdb_id"]: c for c in out}
    assert set(by_id) == {10}                                    # only Show A survives
    assert by_id[10]["years_ago"] == 20 and by_id[10]["season"] == 2 and by_id[10]["episode"] == 5
    assert by_id[10]["owned"] is True and by_id[10]["media"] == "show"


def test_episode_accepts_sonarr_style_field_names():
    rows = [{"series_tvdb_id": 20, "title": "Sonarr Shape", "season_number": 1, "episode_number": 3,
             "air_date_utc": datetime(2000, 1, 1)}]
    out = episode_candidates(rows, _NOW)
    assert out and out[0]["tvdb_id"] == 20 and out[0]["episode"] == 3


def test_candidates_flag_on_this_day():
    rows = [{"tmdb_id": 1, "title": "Exactly Today", "release_date": date(2010, 12, 31), "vote_count": 100},
            {"tmdb_id": 2, "title": "Same Week", "release_date": date(2010, 1, 2), "vote_count": 100}]
    out = {c["tmdb_id"]: c for c in movie_candidates(rows, _NOW)}   # _NOW = 2024-12-31
    assert out[1]["on_this_day"] is True                            # 12-31 == today
    assert out[2]["on_this_day"] is False                          # in the week, but Jan-2 ≠ today


def test_partition_net_new_puts_unowned_first_bucket():
    cands = [{"tmdb_id": 1, "owned": False}, {"tmdb_id": 2, "owned": True}, {"tmdb_id": 3, "owned": False}]
    net_new, owned = partition_net_new(cands)
    assert [c["tmdb_id"] for c in net_new] == [1, 3]
    assert [c["tmdb_id"] for c in owned] == [2]
