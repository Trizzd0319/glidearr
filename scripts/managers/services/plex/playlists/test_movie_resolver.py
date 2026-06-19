"""Tests for the movie playlist resolver — watched filter + join/group/order glue."""
from __future__ import annotations

from datetime import date

from scripts.managers.services.plex.playlists.movie_resolver import (
    build_fresh_movie_plan,
    build_movie_plan,
    movie_inputs,
    watched_movie_keys,
)


# ── watched_movie_keys ────────────────────────────────────────────────────────
def test_watched_movie_keys_finished_only():
    h = [
        {"media_type": "movie", "rating_key": "1", "title": "The Matrix", "year": 1999, "percent_complete": 95},
        {"media_type": "movie", "rating_key": "2", "title": "Half", "year": 2000, "percent_complete": 40},
        {"media_type": "episode", "rating_key": "3", "percent_complete": 99},          # not a movie
    ]
    assert watched_movie_keys(h) == {"1", ("the matrix", 1999)}


# ── build_movie_plan ──────────────────────────────────────────────────────────
def _movie(tmdb, title, year, collection=None, cert=None):
    return {"tmdb_id": tmdb, "title": title, "year": year, "collection_tmdb_id": collection,
            "certification": cert, "in_cinemas_date": f"{year}-01-01"}


def test_resolves_and_orders_by_score():
    owned = [_movie(603, "The Matrix", 1999), _movie(604, "John Wick", 2014)]
    inv = {"603": {"rating_key": "a", "title": "The Matrix"},
           "604": {"rating_key": "b", "title": "John Wick"}}
    plan, stats = build_movie_plan(owned, inv, set(), {603: 30, 604: 90})
    assert [i.rating_key for i in plan.items] == ["b", "a"]      # higher score leads
    assert stats == {"owned": 2, "unresolved": 0, "resolved": 2, "movies": 2, "in_plan": 2}


def test_unresolved_movie_dropped_and_counted():
    owned = [_movie(603, "The Matrix", 1999), _movie(999, "Nope", 2022)]
    inv = {"603": {"rating_key": "a"}}                            # 999 not on this server
    plan, stats = build_movie_plan(owned, inv, set(), {603: 50})
    assert [i.rating_key for i in plan.items] == ["a"]
    assert stats["resolved"] == 1 and stats["unresolved"] == 1


def test_watched_movie_excluded_via_title_year_despite_stale_ratingkey():
    owned = [_movie(603, "The Matrix", 1999), _movie(604, "John Wick", 2014)]
    inv = {"603": {"rating_key": "a"}, "604": {"rating_key": "b"}}
    watched = watched_movie_keys([{"media_type": "movie", "title": "The Matrix",
                                   "year": 1999, "rating_key": "STALE", "percent_complete": 100}])
    plan, _ = build_movie_plan(owned, inv, watched, {603: 90, 604: 50})
    assert [i.rating_key for i in plan.items] == ["b"]           # Matrix dropped by (title,year)


def test_collection_groups_contiguous_in_chrono_order():
    owned = [_movie(1, "Wick 1", 2014, collection=500), _movie(2, "Standalone", 2010),
             _movie(3, "Wick 2", 2017, collection=500)]
    inv = {"1": {"rating_key": "w1"}, "2": {"rating_key": "s"}, "3": {"rating_key": "w2"}}
    plan, _ = build_movie_plan(owned, inv, set(), {1: 80, 2: 40, 3: 80})
    rks = [i.rating_key for i in plan.items]
    assert rks[:2] == ["w1", "w2"]                              # collection contiguous, by release
    assert rks[2] == "s"                                        # lower-scored standalone last


def test_nan_collection_id_is_standalone_not_fused():
    """REGRESSION (review): movie_files numeric columns round-trip a missing value as float
    NaN (not None), and NaN is TRUTHY — without a guard every collection-less movie fuses
    under franchise 'nan' into one bogus group, destroying per-movie ranking. (70% of a
    real library had NaN collection_tmdb_id.)"""
    nan = float("nan")
    owned = [{"tmdb_id": 1, "title": "A", "year": 2000, "collection_tmdb_id": nan},
             {"tmdb_id": 2, "title": "B", "year": 2010, "collection_tmdb_id": nan}]
    inv = {"1": {"rating_key": "a"}, "2": {"rating_key": "b"}}
    plan, _ = build_movie_plan(owned, inv, set(), {1: 50, 2: 90})
    assert [i.rating_key for i in plan.items] == ["b", "a"]      # ranked by score, NOT fused
    assert all(i.group_kind == "standalone" for i in plan.items)


def test_collection_id_intified_for_stable_grouping():
    nan = float("nan")
    owned = [{"tmdb_id": 1, "title": "W1", "year": 2014, "collection_tmdb_id": 500.0, "in_cinemas_date": "2014-01-01"},
             {"tmdb_id": 2, "title": "Solo", "year": 2010, "collection_tmdb_id": nan, "in_cinemas_date": "2010-01-01"},
             {"tmdb_id": 3, "title": "W2", "year": 2017, "collection_tmdb_id": 500.0, "in_cinemas_date": "2017-01-01"}]
    inv = {"1": {"rating_key": "w1"}, "2": {"rating_key": "s"}, "3": {"rating_key": "w2"}}
    plan, _ = build_movie_plan(owned, inv, set(), {1: 80, 2: 40, 3: 80})
    rks = [i.rating_key for i in plan.items]
    assert rks[:2] == ["w1", "w2"]                              # 500.0 groups under key '500'
    assert rks[2] == "s"                                        # NaN-collection movie is standalone


# ── Fresh Arrivals (build_fresh_movie_plan) ─────────────────────────────────────
_NOW = date(2024, 6, 1)


def _fmovie(tmdb, title, year, added_at):
    return {"tmdb_id": tmdb, "title": title, "year": year, "collection_tmdb_id": None,
            "in_cinemas_date": f"{year}-01-01", "added_at": added_at}


def test_added_at_populated_on_movie_inputs():
    inputs, _ = movie_inputs([_fmovie(603, "The Matrix", 1999, "2024-05-20T10:00:00Z")],
                             {"603": {"rating_key": "a"}}, set(), {603: 50})
    assert inputs[0].added_at == "2024-05-20T10:00:00Z"


def test_fresh_plan_filters_to_recent_acquisitions_and_ranks_by_score():
    # An OLD movie (1999) acquired RECENTLY is fresh — freshness is the acquisition date, not the
    # release date; that's the whole point vs Plex's release-blind 'Recently Added'.
    owned = [_fmovie(1, "Recent low", 1999, "2024-05-25"),     # 7d ago → kept
             _fmovie(2, "Recent high", 2021, "2024-05-01"),    # 31d ago → kept
             _fmovie(3, "Stale", 2019, "2024-01-01"),          # >45d → dropped
             _fmovie(4, "Undated", 2018, None)]                # no stamp → dropped (can't prove fresh)
    inv = {str(i): {"rating_key": f"rk{i}"} for i in range(1, 5)}
    plan, stats = build_fresh_movie_plan(owned, inv, set(), {1: 30, 2: 90, 3: 99, 4: 99},
                                         acquired_window_days=45, now=_NOW)
    assert [i.rating_key for i in plan.items] == ["rk2", "rk1"]   # only the fresh two, score-ranked
    assert stats["fresh_candidates"] == 2 and plan.family == "fresh"


def test_fresh_window_boundary_is_inclusive():
    owned = [_fmovie(1, "Edge", 2020, "2024-04-17"),     # exactly 45d before NOW → kept
             _fmovie(2, "JustPast", 2020, "2024-04-16")]  # 46d → dropped
    inv = {"1": {"rating_key": "a"}, "2": {"rating_key": "b"}}
    plan, _ = build_fresh_movie_plan(owned, inv, set(), {1: 50, 2: 50},
                                     acquired_window_days=45, now=_NOW)
    assert [i.rating_key for i in plan.items] == ["a"]


def test_fresh_plan_excludes_watched():
    owned = [_fmovie(1, "Seen", 2020, "2024-05-20"), _fmovie(2, "Unseen", 2021, "2024-05-20")]
    inv = {"1": {"rating_key": "a"}, "2": {"rating_key": "b"}}
    plan, _ = build_fresh_movie_plan(owned, inv, {"a"}, {1: 90, 2: 50},
                                     acquired_window_days=45, now=_NOW)
    assert [i.rating_key for i in plan.items] == ["b"]            # watched 'a' dropped


def test_up_next_byte_identical_whether_added_at_present():
    """Threading added_at through movie_inputs must NOT perturb the up_next plan (recency boost
    is off there) — the new column is inert for the existing playlist."""
    base = [{"tmdb_id": 603, "title": "The Matrix", "year": 1999, "in_cinemas_date": "1999-01-01"},
            {"tmdb_id": 604, "title": "John Wick", "year": 2014, "in_cinemas_date": "2014-01-01"}]
    withd = [dict(m, added_at="2024-05-20") for m in base]
    inv = {"603": {"rating_key": "a"}, "604": {"rating_key": "b"}}
    p0, _ = build_movie_plan(base, inv, set(), {603: 30, 604: 90})
    p1, _ = build_movie_plan(withd, inv, set(), {603: 30, 604: 90})
    assert [(i.rating_key, i.ordinal, i.score) for i in p0.items] == \
           [(i.rating_key, i.ordinal, i.score) for i in p1.items]
