"""Tests for the movie playlist resolver — watched filter + join/group/order glue."""
from __future__ import annotations

from datetime import date

from scripts.managers.services.plex.playlists.movie_resolver import (
    build_fresh_movie_plan,
    build_movie_plan,
    movie_inputs,
    watched_movie_keys,
    watched_movie_recency,
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


def test_real_universe_label_is_fed_but_placeholder_dropped():
    """A REAL universe label (``"mcu"``) becomes a grouping token; the junk bare ``"universe"``
    placeholder is dropped to ``()`` so it can never fuse unrelated movies."""
    owned = [{"tmdb_id": 1, "title": "A", "year": 2008, "universe_name": "mcu"},
             {"tmdb_id": 2, "title": "B", "year": 2011, "universe_name": "universe"},
             {"tmdb_id": 3, "title": "C", "year": 2012, "universe_name": "mcu"}]
    inv = {str(i): {"rating_key": f"rk{i}"} for i in range(1, 4)}
    by_rk = {i.rating_key: i for i in movie_inputs(owned, inv, set(), {})[0]}
    assert by_rk["rk1"].universes == ("mcu",) and by_rk["rk3"].universes == ("mcu",)
    assert by_rk["rk2"].universes == ()                                   # placeholder → no token


def test_universe_label_groups_movies_across_collections():
    # a real universe label binds movies into one contiguous block ordered by release date,
    # even though they sit in different (or no) TMDB collections.
    owned = [{"tmdb_id": 1, "title": "Iron Man", "year": 2008, "universe_name": "mcu", "in_cinemas_date": "2008-01-01"},
             {"tmdb_id": 2, "title": "Thor", "year": 2011, "universe_name": "mcu", "in_cinemas_date": "2011-01-01"}]
    inv = {"1": {"rating_key": "a"}, "2": {"rating_key": "b"}}
    plan, _ = build_movie_plan(owned, inv, set(), {1: 50, 2: 90})
    assert [i.rating_key for i in plan.items] == ["a", "b"]               # contiguous, chrono (not by score)
    assert all(i.group_kind == "universe" and i.group_key == "mcu" for i in plan.items)


def test_pipe_joined_universe_splits_and_bridges_components():
    """REGRESSION: ``universe_name`` stores multi-universe membership pipe-joined ("mcu|xmen").
    The resolver must SPLIT it so an {mcu,xmen} film carries both tokens and bridges the
    pure-mcu and pure-xmen films into ONE connected component (the old code passed "mcu|xmen"
    as a single opaque token, which never bridged)."""
    owned = [{"tmdb_id": 1, "title": "Pure MCU", "year": 2008, "universe_name": "mcu", "in_cinemas_date": "2008-01-01"},
             {"tmdb_id": 2, "title": "Crossover", "year": 2016, "universe_name": "mcu|xmen", "in_cinemas_date": "2016-01-01"},
             {"tmdb_id": 3, "title": "Pure XMen", "year": 2000, "universe_name": "xmen", "in_cinemas_date": "2000-01-01"}]
    inv = {str(i): {"rating_key": f"rk{i}"} for i in range(1, 4)}
    inputs, _ = movie_inputs(owned, inv, set(), {})
    assert {i.rating_key: i.universes for i in inputs}["rk2"] == ("mcu", "xmen")   # split, both tokens
    plan, _ = build_movie_plan(owned, inv, set(), {1: 50, 2: 60, 3: 70})
    # all three land in ONE group (the xmen film bridges via the crossover), contiguous + chrono
    assert {i.group_kind for i in plan.items} == {"universe"}
    assert [i.rating_key for i in plan.items] == ["rk3", "rk1", "rk2"]    # by release date 2000<2008<2016


def test_collection_groups_despite_placeholder_universe():
    # the junk bare "universe" placeholder is dropped, so it never interferes with the real
    # COLLECTION (franchise) binder — collection-500 films still group + name as franchise.
    owned = [{"tmdb_id": 1, "title": "W1", "year": 2014, "collection_tmdb_id": 500, "universe_name": "universe", "in_cinemas_date": "2014-01-01"},
             {"tmdb_id": 2, "title": "Solo", "year": 2010, "in_cinemas_date": "2010-01-01"},
             {"tmdb_id": 3, "title": "W2", "year": 2017, "collection_tmdb_id": 500, "universe_name": "universe", "in_cinemas_date": "2017-01-01"}]
    inv = {"1": {"rating_key": "w1"}, "2": {"rating_key": "s"}, "3": {"rating_key": "w2"}}
    plan, _ = build_movie_plan(owned, inv, set(), {1: 80, 2: 40, 3: 80})
    rks = [i.rating_key for i in plan.items]
    assert rks[:2] == ["w1", "w2"]                                        # collection 500 contiguous
    assert plan.items[0].group_kind == "franchise" and rks[2] == "s"


def test_universe_movies_do_not_starve_the_cap():
    """End-to-end acceptance: a block of ``universe``-tagged movies (which used to fuse into one
    cap-starving mega-group) plus a higher-ranked real 2-movie collection. The plan must fill
    the whole budget, not collapse to the items that rank ahead of the former mega-group."""
    owned = [{"tmdb_id": i, "title": f"U{i}", "year": 2000 + i, "universe_name": "universe",
              "in_cinemas_date": f"{2000 + i}-01-01"} for i in range(1, 9)]        # 8 universe-tagged
    inv = {str(i): {"rating_key": f"u{i}"} for i in range(1, 9)}
    scores = {i: 80 for i in range(1, 9)}
    owned += [{"tmdb_id": 100, "title": "C1", "year": 2014, "collection_tmdb_id": 500, "in_cinemas_date": "2014-01-01"},
              {"tmdb_id": 101, "title": "C2", "year": 2017, "collection_tmdb_id": 500, "in_cinemas_date": "2017-01-01"}]
    inv |= {"100": {"rating_key": "c1"}, "101": {"rating_key": "c2"}}
    scores |= {100: 95, 101: 95}                                            # collection ranks first
    plan, _ = build_movie_plan(owned, inv, set(), scores, max_items=5)
    # collection(2) + 3 standalone movies = 5. Pre-fix: the 8 fused into one group,
    # collection(2)+group(8) > 5 stopped at 2 items.
    assert len(plan.items) == 5
    assert plan.items[0].group_key == "500"                                # real collection leads


# ── universe timeline ordering (Kometa-collection saga order) ───────────────────
def test_universe_order_populates_timeline_index():
    owned = [{"tmdb_id": 1, "title": "A", "year": 2008, "universe_name": "mcu"},
             {"tmdb_id": 2, "title": "B", "year": 2019, "universe_name": "mcu"}]
    inv = {"1": {"rating_key": "a"}, "2": {"rating_key": "b"}}
    inputs, _ = movie_inputs(owned, inv, set(), {}, universe_order={2: 0})
    by = {i.rating_key: i for i in inputs}
    assert by["b"].timeline_index == 0 and by["a"].timeline_index is None   # unlisted → date path


def test_universe_order_overrides_release_date_within_block():
    # Within the mcu block, the curated saga order (Captain Marvel — set 1995 — BEFORE Iron Man)
    # overrides release-date ordering (CM released 2019, after Iron Man 2008).
    owned = [{"tmdb_id": 1, "title": "Iron Man", "year": 2008, "universe_name": "mcu", "in_cinemas_date": "2008-01-01"},
             {"tmdb_id": 2, "title": "Captain Marvel", "year": 2019, "universe_name": "mcu", "in_cinemas_date": "2019-01-01"}]
    inv = {"1": {"rating_key": "iron"}, "2": {"rating_key": "cm"}}
    plan, _ = build_movie_plan(owned, inv, set(), {1: 80, 2: 80}, universe_order={2: 0, 1: 1})
    assert [i.rating_key for i in plan.items] == ["cm", "iron"]   # saga order, NOT release (2008<2019)
    assert all(i.group_kind == "universe" for i in plan.items)


def test_universe_membership_groups_without_any_tag():
    """Kometa-independence: with NO ``universe_name`` tag at all, the list-sourced membership
    forms the MCU group and the order saga-orders it — proving the playlist no longer needs Kometa."""
    owned = [{"tmdb_id": 1, "title": "Iron Man", "year": 2008, "in_cinemas_date": "2008-01-01"},
             {"tmdb_id": 2, "title": "Thor", "year": 2011, "in_cinemas_date": "2011-01-01"}]
    inv = {"1": {"rating_key": "a"}, "2": {"rating_key": "b"}}
    plan, _ = build_movie_plan(owned, inv, set(), {1: 50, 2: 90},
                               universe_membership={1: {"mcu"}, 2: {"mcu"}}, universe_order={1: 0, 2: 1})
    assert all(i.group_kind == "universe" and i.group_key == "mcu" for i in plan.items)   # grouped, no tag
    assert [i.rating_key for i in plan.items] == ["a", "b"]            # saga order (1<2), not by score


def test_membership_unions_with_universe_name_tag():
    # a Kometa tag ('xmen') AND a list membership ('mcu') on the same film → BOTH tokens (bridges).
    owned = [{"tmdb_id": 1, "title": "Deadpool & Wolverine", "year": 2024, "universe_name": "xmen"}]
    inv = {"1": {"rating_key": "dw"}}
    inputs, _ = movie_inputs(owned, inv, set(), {}, universe_membership={1: {"mcu"}})
    assert inputs[0].universes == ("mcu", "xmen")                     # tag ∪ list, sorted


# ── resume boost (continue an in-progress movie saga) ───────────────────────────
def test_watched_movie_recency_takes_latest_and_stamps_last_watched():
    h = [{"media_type": "movie", "rating_key": "1", "title": "The Matrix", "year": 1999, "percent_complete": 95, "date": 1000},
         {"media_type": "movie", "rating_key": "1", "title": "The Matrix", "year": 1999, "percent_complete": 95, "date": 2000}]
    rec = watched_movie_recency(h)
    assert rec["1"] == 2000 and rec[("the matrix", 1999)] == 2000        # latest rewatch wins
    inputs, _ = movie_inputs([_movie(603, "The Matrix", 1999)], {"603": {"rating_key": "1"}},
                             {"1"}, {603: 50}, watch_recency=rec)
    assert inputs[0].last_watched == 2000                                # stamped on the watched item


def test_resume_weight_tunes_saga_vs_standalone():
    owned = [_movie(1, "Freddy 1", 1984, collection=700), _movie(2, "Freddy 2", 1985, collection=700),
             _movie(3, "Passengers", 2016), _movie(4, "Other", 2010)]   # 'Other' spreads normalization
    inv = {"1": {"rating_key": "f1"}, "2": {"rating_key": "f2"}, "3": {"rating_key": "p"}, "4": {"rating_key": "o"}}
    watched, scores = {"f1"}, {1: 50, 2: 50, 3: 90, 4: 20}              # mid-Freddy; Passengers high
    p0, _ = build_movie_plan(owned, inv, watched, scores, resume_boost=True, resume_weight=0.0)
    assert p0.items[0].rating_key == "p"                               # weight 0 → Passengers' affinity wins
    p1, _ = build_movie_plan(owned, inv, watched, scores, resume_boost=True, resume_weight=1.0)
    assert p1.items[0].rating_key == "f2"                              # strong weight → resume Freddy first


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
