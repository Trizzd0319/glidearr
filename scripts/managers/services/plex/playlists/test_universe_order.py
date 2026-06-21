"""Tests for universe_order — pure saga-order + TV-franchise derivation from Plex collections."""
from __future__ import annotations

from scripts.managers.services.plex.playlists.universe_order import (
    build_universe_maps,
    collection_universe_key,
    is_stale,
    merge_movie_orders,
    movie_order_from_children,
    movie_universe_keys,
    saga_display_name,
    saga_member_sets,
    saga_membership_index,
    series_order_from_children,
    split_list_media,
    stem_franchise_clusters,
    tv_franchise_maps,
    tv_franchise_universes,
    tv_group_maps,
    tv_group_maps_from_series,
    unified_universe_order,
    universe_acquire_plan,
    universe_lists,
)


# ── collection title → universe key ────────────────────────────────────────────
def test_collection_universe_key_matches_kometa_names():
    assert collection_universe_key("Marvel Cinematic Universe") == "mcu"
    assert collection_universe_key("  star wars universe ") == "star"     # strip + casefold
    assert collection_universe_key("X-Men Universe") == "xmen"
    assert collection_universe_key("Some Random Collection") is None
    assert collection_universe_key(None) is None


# ── saga key → human label, and id → saga reverse index (acquisition attribution) ─
def test_saga_display_name():
    assert saga_display_name("mcu") == "Marvel Cinematic Universe"   # reverse of the Kometa map
    assert saga_display_name("one chicago") == "One Chicago"          # curated key, Title-Cased
    assert saga_display_name("tvfran:ncis") == "Ncis"                 # auto-cluster prefix stripped
    assert saga_display_name("") == "" and saga_display_name(None) == ""


def test_saga_membership_index_reverse_lookup():
    src = {"universes": {
        "mcu": {"timeline": True, "items": [
            {"media": "movie", "tmdb": 1}, {"media": "show", "tvdb": 100}]},
        "avengers": {"timeline": True, "items": [{"media": "movie", "tmdb": 1}]},  # crossover film
    }}
    idx = saga_membership_index(src)
    assert set(idx[("movie", 1)]) == {"mcu", "avengers"}    # film in two sagas
    assert idx[("show", 100)] == ["mcu"]
    assert ("movie", 999) not in idx                        # unlisted title → absent
    assert saga_membership_index(None) == {} and saga_membership_index({}) == {}


# ── movie order from a collection's ordered children ────────────────────────────
def test_movie_order_dense_positions_over_owned_only():
    # children in collection (saga) order; rk "9" is NOT owned → skipped, positions stay dense.
    children = ["100", "9", "200", "300"]
    rk_to_tmdb = {"100": 11, "200": 22, "300": 33}           # 9 absent (un-owned)
    assert movie_order_from_children(children, rk_to_tmdb) == {11: 0, 22: 1, 33: 2}


def test_movie_order_first_occurrence_wins():
    children = ["100", "200", "100"]
    assert movie_order_from_children(children, {"100": 11, "200": 22}) == {11: 0, 22: 1}


def test_movie_order_allowed_tmdbs_filters_and_stays_dense():
    # only tmdbs in allowed get a position; positions stay dense over the survivors.
    children = ["a", "b", "c"]
    rk_to_tmdb = {"a": 11, "b": 22, "c": 33}
    assert movie_order_from_children(children, rk_to_tmdb, allowed_tmdbs={11, 33}) == {11: 0, 33: 1}


# ── movie_universe_keys (Radarr universe_name → membership for the order filter) ─
def test_movie_universe_keys_splits_and_drops_placeholder():
    owned = [{"tmdb_id": 1, "universe_name": "mcu|xmen"},
             {"tmdb_id": 2, "universe_name": "universe"},        # placeholder → no keys → absent
             {"tmdb_id": 3, "universe_name": "MCU"},             # casefolded
             {"tmdb_id": 4}]                                     # no universe_name → absent
    assert movie_universe_keys(owned) == {1: {"mcu", "xmen"}, 3: {"mcu"}}


def test_movie_order_empty_safe():
    assert movie_order_from_children([], {"1": 1}) == {}
    assert movie_order_from_children(["1"], {}) == {}


def test_merge_movie_orders_flat_union_later_wins():
    a = {11: 0, 22: 1}
    b = {33: 0, 11: 5}                                        # 11 overlaps → later wins
    assert merge_movie_orders([a, b]) == {11: 5, 22: 1, 33: 0}


# ── curated TV franchise maps ───────────────────────────────────────────────────
def test_tv_franchise_maps_binds_and_orders_one_chicago():
    owned = [(1, "Chicago Fire"), (2, "Chicago P.D."), (3, "Chicago Med"), (9, "Bluey")]
    fran, time = tv_franchise_maps(owned)
    assert fran == {1: "one chicago", 2: "one chicago", 3: "one chicago"}   # Bluey unmatched
    assert time == {1: 0, 2: 1, 3: 2}                                       # saga order


def test_tv_franchise_maps_ignores_year_suffix_in_title():
    fran, _ = tv_franchise_maps([(7, "Chicago Fire (2012)")])
    assert fran == {7: "one chicago"}                          # "(2012)" stripped before match


def test_tv_franchise_maps_custom_table():
    curated = {"trek tv": ["Star Trek: Discovery", "Star Trek: Picard"]}
    fran, time = tv_franchise_maps([(5, "Star Trek: Picard"), (6, "Unrelated")], curated)
    assert fran == {5: "trek tv"} and time == {5: 1}


def test_tv_group_maps_timeline_index_when_available_else_airdate():
    # timeline_index when available: curated One Chicago (Fire=0, P.D.=1) AND timeline-universe MCU
    # both carry one; a release-ordered universe-list franchise carries grouping only → air date.
    source = {"universes": {
        "mcu": {"timeline": True, "movies": [], "shows": [600, 601]},
        "fast": {"timeline": False, "movies": [], "shows": [700]},   # release-ordered → no timeline
    }}
    owned = [(1, "Chicago Fire"), (2, "Chicago P.D."), (10, "Loki"), (11, "Daredevil"), (70, "Fast Show")]
    fran, time = tv_group_maps(owned, source, {600: 10, 601: 11, 700: 70})
    assert fran == {1: "one chicago", 2: "one chicago", 10: "mcu", 11: "mcu", 70: "fast"}
    assert time == {1: 0, 2: 1, 10: 0, 11: 1}          # curated One Chicago + timeline MCU
    assert 70 not in time                              # release-ordered universe franchise → air date


def test_tv_group_maps_from_series_splits_raw_rows():
    # The Sonarr-layer accessor input: raw series dicts (id/title/tvdbId) → same maps as tv_group_maps.
    rows = [{"id": 1, "title": "Chicago Fire", "tvdbId": None},
            {"id": 2, "title": "Chicago P.D.", "tvdbId": None},
            {"id": 10, "title": "Loki", "tvdbId": 600},
            {"id": 99, "title": "Unrelated", "tvdbId": 700}]
    source = {"universes": {"mcu": {"timeline": True, "movies": [], "shows": [600]}}}
    fran, time = tv_group_maps_from_series(rows, source)
    assert fran == {1: "one chicago", 2: "one chicago", 10: "mcu"}   # 99 ungrouped (absent)
    assert time == {1: 0, 2: 1, 10: 0}                               # curated One Chicago + timeline MCU


def test_tv_group_maps_list_wins_over_curated():
    # A series both curated AND in a timeline universe list -> the list grouping + timeline wins.
    source = {"universes": {"trek": {"timeline": True, "movies": [], "shows": [700]}}}
    curated = {"trek tv": ["Star Trek: Picard"]}
    fran, time = tv_group_maps([(5, "Star Trek: Picard")], source, {700: 5}, curated=curated)
    assert fran == {5: "trek"} and time == {5: 0}      # universe list wins, contributes timeline


# ── hybrid: bundled definitions + mdblist list source → maps ────────────────────
def test_universe_lists_merges_config_overrides():
    base = universe_lists()
    assert base["mcu"] == {"id": 117444, "timeline": True}
    over = universe_lists({"mcu": {"mdblist": "me/mine", "timeline": False},
                           "newverse": {"imdb": "ls999", "timeline": True}})
    assert over["mcu"] == {"mdblist": "me/mine", "timeline": False}   # override wins
    assert over["newverse"]["imdb"] == "ls999"                        # brand-new universe added


def test_split_list_media_partitions_in_order():
    items = [{"tmdb": 1, "tvdb": None, "media": "movie"},
             {"tmdb": None, "tvdb": 50, "media": "show"},
             {"tmdb": 2, "tvdb": None, "media": "movie"}]
    out = split_list_media(items, True)
    assert out["timeline"] is True and out["movies"] == [1, 2] and out["shows"] == [50]
    assert out["items"] == [{"media": "movie", "tmdb": 1, "rank": 0},   # UNIFIED cross-media rank
                            {"media": "show", "tvdb": 50, "rank": 1},
                            {"media": "movie", "tmdb": 2, "rank": 2}]


def test_unified_universe_order_interleaves_movies_and_shows_owned_only():
    source = {"universes": {"mcu": {"timeline": True, "items": [
        {"media": "movie", "tmdb": 10, "rank": 0},
        {"media": "show", "tvdb": 500, "rank": 1},
        {"media": "movie", "tmdb": 11, "rank": 2},     # not owned → dropped (owned-only)
        {"media": "show", "tvdb": 501, "rank": 3}]}}}
    out = unified_universe_order(source, {10}, {500: 7, 501: 8})
    assert out["mcu"] == [
        {"media": "movie", "id": 10, "rank": 0, "owned": True},
        {"media": "show", "id": 500, "rank": 1, "owned": True},
        {"media": "show", "id": 501, "rank": 2, "owned": True}]      # re-ranked densely


def test_unified_universe_order_include_unowned_keeps_gaps_in_order():
    source = {"universes": {"mcu": {"timeline": True, "items": [
        {"media": "movie", "tmdb": 10, "rank": 0},
        {"media": "movie", "tmdb": 11, "rank": 1},     # gap (unowned)
        {"media": "show", "tvdb": 500, "rank": 2}]}}}
    out = unified_universe_order(source, {10}, {500: 7}, include_unowned=True)
    assert out["mcu"] == [
        {"media": "movie", "id": 10, "rank": 0, "owned": True},
        {"media": "movie", "id": 11, "rank": 1, "owned": False},    # acquire candidate, in order
        {"media": "show", "id": 500, "rank": 2, "owned": True}]


def test_unified_universe_order_skips_non_timeline_and_stale_source():
    src = {"universes": {
        "fast": {"timeline": False, "items": [{"media": "movie", "tmdb": 1, "rank": 0}]},  # release
        "old":  {"timeline": True, "movies": [1], "shows": []}}}                           # no 'items'
    assert unified_universe_order(src, {1}, {}) == {}


def test_universe_acquire_plan_backfills_engaged_saga_start_first():
    # Star Wars timeline: Ep I (m100), Ep II (m101), Clone Wars (show 500), Ep III (m102).
    # Household watched ONLY Clone Wars (mid-saga) and owns just it → backfill the films, START
    # first (Ep I/II ahead of Ep III), rank-ascending. Clone Wars' own continuation is the
    # next-episode walk's job, not here.
    unified = unified_universe_order(
        {"universes": {"star": {"timeline": True, "items": [
            {"media": "movie", "tmdb": 100, "rank": 0},
            {"media": "movie", "tmdb": 101, "rank": 1},
            {"media": "show", "tvdb": 500, "rank": 2},
            {"media": "movie", "tmdb": 102, "rank": 3}]}}},
        owned_movie_tmdbs=set(), owned_tvdb_to_sid={500: 9}, include_unowned=True)
    plan = universe_acquire_plan(unified, watched_movie_tmdbs=set(), watched_show_tvdbs={500})
    assert plan["star"] == [                                  # films only, rank-ascending (start first)
        {"media": "movie", "id": 100, "rank": 0},
        {"media": "movie", "id": 101, "rank": 1},
        {"media": "movie", "id": 102, "rank": 3}]


def test_universe_acquire_plan_skips_unengaged_saga():
    # Household watched NOTHING in the saga → no cold-start, no acquire.
    unified = unified_universe_order(
        {"universes": {"mcu": {"timeline": True, "items": [
            {"media": "movie", "tmdb": 10, "rank": 0},
            {"media": "show", "tvdb": 500, "rank": 1}]}}},
        owned_movie_tmdbs=set(), owned_tvdb_to_sid={}, include_unowned=True)
    assert universe_acquire_plan(unified, set(), set()) == {}


def test_is_stale_ttl_boundary():
    assert is_stale(None, 100, 7) is True            # never fetched
    assert is_stale(100, 106, 7) is False            # 6 days < ttl
    assert is_stale(100, 107, 7) is True             # 7 days >= ttl → refetch


def test_build_universe_maps_membership_and_order_owned_only():
    source = {"universes": {
        "mcu": {"timeline": True, "movies": [10, 99, 20], "shows": [500]},   # 99 not owned
        "fast": {"timeline": False, "movies": [30], "shows": []},            # release-ordered
    }}
    owned_movies = {10, 20, 30}
    tvdb_to_sid = {500: 7}
    mem, order, fran, time = build_universe_maps(source, owned_movies, tvdb_to_sid)
    assert mem == {10: {"mcu"}, 20: {"mcu"}, 30: {"fast"}}     # membership for all owned
    assert order == {10: 0, 20: 1}                            # dense over owned; fast=release→no order
    assert fran == {7: "mcu"} and time == {7: 0}              # owned show grouped + timed


def test_build_universe_maps_multi_universe_keeps_all_keys():
    source = {"universes": {"mcu": {"timeline": True, "movies": [1, 2]},
                            "xmen": {"timeline": True, "movies": [2, 3]}}}
    mem, order, _, _ = build_universe_maps(source, {1, 2, 3}, {})
    assert mem[2] == {"mcu", "xmen"}                          # crossover bridges both universes
    assert order[2] == 1                                      # first universe's (mcu) position wins


# ── TV series order from a Kometa TV-universe collection (show ratingKeys) ───────
def test_series_order_from_children_universe_grouping_and_timeline():
    children = ["s100", "s_unowned", "s200"]                   # show ratingKeys in collection order
    show_rk_to_series = {"s100": 10, "s200": 20}              # s_unowned not owned → skipped
    fran, time = series_order_from_children(children, show_rk_to_series, "arrowverse")
    assert fran == {10: "arrowverse", 20: "arrowverse"}
    assert time == {10: 0, 20: 1}                              # custom-order universe → timeline


def test_series_order_franchise_collection_grouping_only():
    # a release-ordered FRANCHISE show collection contributes GROUPING but NO timeline_index
    children = ["s1", "s2"]
    fran, time = series_order_from_children(children, {"s1": 1, "s2": 2}, "star trek",
                                            with_timeline=False)
    assert fran == {1: "star trek", 2: "star trek"} and time == {}


# ── saga_member_sets: full ownership-independent membership + rank (retention gate) ──
def test_saga_member_sets_from_unified_items_one_rank_axis():
    # unified cross-media items → movies + shows share ONE rank axis, ownership-independent.
    source = {"universes": {"mcu": {"timeline": True, "items": [
        {"media": "movie", "tmdb": 1}, {"media": "show", "tvdb": 100}, {"media": "movie", "tmdb": 2}]}}}
    assert saga_member_sets(source) == {"mcu": {"movies": {1: 0, 2: 2}, "shows": {100: 1}}}


def test_saga_member_sets_from_legacy_lists_movies_then_shows():
    # no unified items → rank movies first, then shows (continuing the axis).
    source = {"universes": {"sw": {"movies": [10, 11], "shows": [200]}}}
    assert saga_member_sets(source) == {"sw": {"movies": {10: 0, 11: 1}, "shows": {200: 2}}}


def test_saga_member_sets_empty_universe_skipped():
    assert saga_member_sets({"universes": {"empty": {"movies": [], "shows": []}}}) == {}
    assert saga_member_sets({}) == {}


# ── Layer-1 same-name TV-franchise clustering (runtime, owned inventory) ─────────────
_FAM = [
    {"title": "Law & Order", "tvdbId": 1},
    {"title": "Law & Order: Special Victims Unit", "tvdbId": 2},
    {"title": "Law & Order: Organized Crime", "tvdbId": 3},
    {"title": "NCIS", "tvdbId": 10},
    {"title": "NCIS: Hawai'i", "tvdbId": 11},                 # accent + apostrophe fold
    {"title": "Chicago Fire", "tvdbId": 20},                  # no subtitle → leading-token class
    {"title": "Chicago Med", "tvdbId": 21},
    {"title": "Chicago P.D.", "tvdbId": 22},
    {"title": "9-1-1", "tvdbId": 30},                         # hyphen-digit, no space delimiter
    {"title": "9-1-1: Lone Star", "tvdbId": 31},
    {"title": "Breaking Bad", "tvdbId": 40},                  # standalone → no cluster (catalog's job)
]


def test_stem_clusters_subtitle_and_leading_token():
    out = stem_franchise_clusters(_FAM)
    assert out["tvfran:laworder"] == [1, 2, 3]
    assert out["tvfran:ncis"] == [10, 11]
    assert out["tvfran:911"] == [30, 31]
    assert out["tvfran:chicago"] == [20, 21, 22]             # leading-token cluster (stems all differ)
    assert "tvfran:breakingbad" not in out and "tvfran:breaking" not in out   # standalone dropped


def test_stem_clusters_dedup_and_singletons():
    rows = [{"title": "Fargo", "tvdbId": 5}, {"title": "Fargo", "tvdbId": 5},   # dup tvdb
            {"title": "Severance", "tvdbId": 6}]
    assert stem_franchise_clusters(rows) == {}                # one real series each → no cluster


def test_stem_clusters_deny_blocks_regional_remakes():
    rows = [{"title": "The Office (US)", "tvdbId": 7}, {"title": "The Office (UK)", "tvdbId": 8}]
    assert stem_franchise_clusters(rows) == {}               # 'office'/'theoffice' in the DENY set
    # an explicit deny arg also blocks a stem cluster
    sham = [{"title": "Shameless", "tvdbId": 7}, {"title": "Shameless", "tvdbId": 8}]
    assert stem_franchise_clusters(sham, deny={"shameless"}) == {}


# ── tv_franchise_universes: the synthetic universe-source seam (Phase 1) ──────────────
def test_tv_franchise_universes_emits_timeline_true_show_entries():
    out = tv_franchise_universes(_FAM, catalog={})
    lo = out["tvfran:laworder"]
    assert lo["timeline"] is True                              # REQUIRED — unified_universe_order skips falsy
    assert lo["movies"] == [] and lo["shows"] == [1, 2, 3]     # TV-only, input (debut) order
    assert lo["items"] == [{"media": "show", "tvdb": 1, "rank": 0},
                           {"media": "show", "tvdb": 2, "rank": 1},
                           {"media": "show", "tvdb": 3, "rank": 2}]
    assert set(out) == {"tvfran:laworder", "tvfran:ncis", "tvfran:911", "tvfran:chicago"}


def test_tv_franchise_universes_orders_members_by_debut():
    rows = [{"title": "Star Trek: Picard", "tvdbId": 3, "year": 2020},
            {"title": "Star Trek: The Next Generation", "tvdbId": 1, "year": 1987},
            {"title": "Star Trek: Voyager", "tvdbId": 2, "tvdb_first_aired": "1995-01-16"}]
    e = tv_franchise_universes(rows, catalog={})["tvfran:startrek"]
    assert e["shows"] == [1, 2, 3]                             # 1987 < 1995 < 2020 (debut asc)
    assert [it["tvdb"] for it in e["items"]] == [1, 2, 3] and e["items"][0]["rank"] == 0


def test_tv_franchise_universes_undated_members_sort_last_stable():
    rows = [{"title": "X: B", "tvdbId": 2},                    # undated
            {"title": "X: A", "tvdbId": 1, "year": 2000}]      # dated
    assert tv_franchise_universes(rows, catalog={})["tvfran:x"]["shows"] == [1, 2]   # dated first


def test_tv_franchise_universes_empty_when_no_family_or_clustering_off():
    singles = [{"title": "Fargo", "tvdbId": 5}, {"title": "Severance", "tvdbId": 6}]
    assert tv_franchise_universes(singles, catalog={}) == {}
    assert tv_franchise_universes(_FAM, catalog={}, cluster_same_stem=False) == {}   # Layer-1 off + empty catalog


def test_tv_franchise_universes_layer2_catalog_scoped_to_owned_or_watchlisted():
    cat = {"buffyverse": {"shows": [101, 102]}, "stargate": {"shows": [201, 202]},
           "tvfran:cold": [301, 302]}
    owned = [{"title": "Buffy", "tvdbId": 101}]                # own 1 of buffyverse
    out = tv_franchise_universes(owned, cat, engaged_tvdbs={202})   # watchlisted 1 of stargate (UNOWNED)
    assert out["tvfran:buffyverse"]["shows"] == [101, 102]     # owned member → emitted, incl. unowned 102
    assert out["tvfran:buffyverse"]["timeline"] is True
    assert out["tvfran:stargate"]["shows"] == [201, 202]       # watchlist intent → emitted (own none of it)
    assert "tvfran:cold" not in out                            # neither owned nor watchlisted → excluded


def test_tv_franchise_universes_empty_catalog_is_noop():
    assert tv_franchise_universes([{"title": "Solo Show", "tvdbId": 5}], {}) == {}


def test_tv_franchise_universes_entries_round_trip_through_consumers():
    # the integration guard the seam-map flagged as missing: producer output → every consumer.
    src = {"universes": tv_franchise_universes(_FAM, catalog={})}
    # playlist grouping (build_universe_maps reads `shows`, stamps order because timeline True)
    _, _, fran, time = build_universe_maps(src, set(), {1: 100, 2: 101, 3: 102})
    assert fran == {100: "tvfran:laworder", 101: "tvfran:laworder", 102: "tvfran:laworder"}
    assert time == {100: 0, 101: 1, 102: 2}                    # debut/input order preserved
    # retention (saga_member_sets reads `items`)
    assert saga_member_sets(src)["tvfran:laworder"] == {"movies": {}, "shows": {1: 0, 2: 1, 3: 2}}
    # acquisition (unified_universe_order requires timeline truthy → franchise gaps are seen)
    uni = unified_universe_order(src, set(), {1: 100}, include_unowned=True)["tvfran:laworder"]
    assert [(m["media"], m["id"], m["owned"]) for m in uni] == [
        ("show", 1, True), ("show", 2, False), ("show", 3, False)]
