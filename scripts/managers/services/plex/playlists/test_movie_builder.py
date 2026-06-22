"""Tests for the per-user MOVIE playlist builder core (_build_for_users)."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from scripts.managers.services.plex.playlists.movie_builder import (
    _FRESH_PLAN_KEY,
    _PLAN_KEY,
    _PROTECTED_KEY,
    MoviePlaylistBuilderManager,
)


@pytest.fixture(autouse=True)
def _isolate_universe_timeline_bake(monkeypatch):
    """Unit tests must not pick up the committed ``universe_timeline.json`` chronolists bake — restrict
    ``_universe_timeline_catalog`` to each test's config overlay so the test fully controls the timeline."""
    def _cfg_only(self):
        ov = (self._pl_cfg().get("universe_timeline", {}) or {}).get("universes", {})
        return dict(ov) if isinstance(ov, dict) else {}
    monkeypatch.setattr(MoviePlaylistBuilderManager, "_universe_timeline_catalog", _cfg_only)


class _Log:
    def __init__(self): self.infos = []; self.warns = []; self.grids = []
    def log_info(self, m): self.infos.append(m)
    def log_warning(self, m): self.warns.append(m)
    def log_error(self, m): pass
    def log_grid(self, headers, rows, title="", cap=16): self.grids.append((title, rows))


class _Cache:
    def __init__(self): self.d = {}
    def get(self, k): return self.d.get(k)
    def set(self, k, v): self.d[k] = v


def _mgr(cache=None, config=None):
    m = MoviePlaylistBuilderManager.__new__(MoviePlaylistBuilderManager)
    m.global_cache = cache
    m.logger = _Log()
    m.config = config if config is not None else {}
    m.registry = None
    m.dry_run = False
    return m


def _movie(tmdb, title, year, score, genres=None, cert=None):
    return {"tmdb_id": tmdb, "title": title, "year": year, "watchability_score": score,
            "genres": genres, "certification": cert, "in_cinemas_date": f"{year}-01-01"}


_TRACKED = [{"safe_user": "rob", "title": "Rob"}]


def _items(cache, su):
    return [it["rating_key"] for it in cache.get(f"{_PLAN_KEY}/{su}")["items"]]


class _FakePlexAPI:
    """Minimal Plex API stub: one universe collection ('Marvel Cinematic Universe') whose
    children come back IN COLLECTION ORDER (Captain Marvel before Iron Man = saga order)."""
    def get_collections(self, section_id=None):
        return {"MediaContainer": {"Metadata": [
            {"ratingKey": "c1", "title": "Marvel Cinematic Universe"},
            {"ratingKey": "c2", "title": "Some Random Collection"}]}}      # non-universe → ignored

    def get_collection_children(self, rk):
        if rk == "c1":
            return {"MediaContainer": {"Metadata": [{"ratingKey": "cm"}, {"ratingKey": "iron"}]}}
        return {"MediaContainer": {"Metadata": []}}


_ON = {"plex": {"playlists": {"universe_timeline": {"enabled": True}}}}


def test_universe_timeline_off_by_default_yields_empty_maps():
    m = _mgr()
    assert m._movie_universe_order({"1": {"rating_key": "a"}}) == {}     # flag off → no read
    assert m._tv_franchise_maps([{"series_id": 1, "series_title": "Chicago Fire"}]) == ({}, {})


def test_movie_universe_order_reads_collection_in_saga_order():
    m = _mgr(config=_ON)
    m.plex_api = _FakePlexAPI()
    inv = {"603": {"rating_key": "iron"}, "604": {"rating_key": "cm"}}   # tmdb → ratingKey
    owned = [{"tmdb_id": 603, "universe_name": "mcu"}, {"tmdb_id": 604, "universe_name": "mcu"}]
    # collection order is [cm, iron] → cm(604) gets pos 0, iron(603) pos 1 (saga, not release)
    assert m._movie_universe_order(inv, owned) == {604: 0, 603: 1}


def test_movie_universe_order_skips_movie_not_tagged_for_that_universe():
    # 'foreign' is a child of the MCU collection but its Radarr universe_name is only 'xmen' →
    # it must NOT inherit an MCU saga index (would mis-order it inside its xmen group).
    class _API:
        def get_collections(self, section_id=None):
            return {"MediaContainer": {"Metadata": [
                {"ratingKey": "c1", "title": "Marvel Cinematic Universe"}]}}
        def get_collection_children(self, rk):
            return {"MediaContainer": {"Metadata": [{"ratingKey": "real"}, {"ratingKey": "foreign"}]}}
    m = _mgr(config=_ON)
    m.plex_api = _API()
    inv = {"100": {"rating_key": "real"}, "200": {"rating_key": "foreign"}}
    owned = [{"tmdb_id": 100, "universe_name": "mcu"}, {"tmdb_id": 200, "universe_name": "xmen"}]
    assert m._movie_universe_order(inv, owned) == {100: 0}              # xmen-only film excluded


# ── TV: Plex SHOW-collection order reader (Phase 1b parity with the movie reader) ──
def test_tvdb_from_guids_parses_modern_legacy_and_misses():
    f = MoviePlaylistBuilderManager._tvdb_from_guids
    assert f({"Guid": [{"id": "tvdb://12345"}]}) == 12345
    assert f({"Guid": [{"id": "imdb://tt1"}, {"id": "tvdb://77"}]}) == 77        # picks the tvdb one
    assert f({"guid": "com.plexapp.agents.thetvdb://9001?lang=en"}) == 9001      # legacy agent form
    assert f({"Guid": [{"id": "tmdb://550"}]}) is None                           # no tvdb present
    assert f({"Guid": [], "guid": "plex://show/abc"}) is None                    # bare plex:// → no hop
    assert f({}) is None


class _FakeTVPlexAPI:
    """One universe SHOW collection ('Marvel Cinematic Universe') whose children come back IN
    COLLECTION ORDER (Loki before WandaVision) carrying tvdb Guids; one unowned show + a non-universe
    collection that must be ignored."""
    def get_collections(self, section_id=None):
        return {"MediaContainer": {"Metadata": [
            {"ratingKey": "u1", "title": "Marvel Cinematic Universe"},
            {"ratingKey": "u2", "title": "Some Random Collection"}]}}           # non-universe → ignored

    def get_collection_children(self, rk, include_guids=False):
        if rk == "u1":
            return {"MediaContainer": {"Metadata": [
                {"ratingKey": "loki",   "Guid": [{"id": "tvdb://400000"}]},
                {"ratingKey": "wanda",  "Guid": [{"id": "tvdb://400001"}]},
                {"ratingKey": "extern", "Guid": [{"id": "tvdb://999999"}]}]}}    # unowned → no position
        return {"MediaContainer": {"Metadata": []}}


def test_plex_tv_collection_order_orders_owned_series_by_collection():
    m = _mgr()
    m.plex_api = _FakeTVPlexAPI()
    tvdb_to_sid = {400000: 11, 400001: 22}                                       # owns Loki + WandaVision
    fran, timeline = m._plex_tv_collection_order(tvdb_to_sid)
    assert fran == {11: "mcu", 22: "mcu"}                                        # both grouped under the universe
    assert timeline == {11: 0, 22: 1}                                           # collection order → timeline_index
    # unowned show consumed no position (dense over owned survivors)


def test_plex_tv_collection_order_empty_without_api_or_series():
    assert MoviePlaylistBuilderManager._plex_tv_collection_order(_mgr(), {1: 2}) == ({}, {})  # no plex_api
    m = _mgr(); m.plex_api = _FakeTVPlexAPI()
    assert m._plex_tv_collection_order({}) == ({}, {})                           # no owned series


def _on_with_list_and_key():
    return {"plex": {"playlists": {"universe_timeline": {"enabled": True}}}, "mdblist": {"apikey": "k"}}


def test_universe_membership_and_order_from_mdblist_list(monkeypatch):
    import scripts.managers.services.plex.playlists.builder as B
    # the MCU list returns two films in saga order [200, 100]; other universes are empty
    monkeypatch.setattr(B.mdblist_client, "list_items", lambda key, defn:
        {"ok": True, "items": [{"tmdb": 200, "tvdb": None, "media": "movie"},
                               {"tmdb": 100, "tvdb": None, "media": "movie"}]}
        if defn.get("id") == 117444 else {"ok": True, "items": []})
    m = _mgr(cache=_Cache(), config=_on_with_list_and_key())
    owned = [{"tmdb_id": 100}, {"tmdb_id": 200}, {"tmdb_id": 300}]      # 300 not in any list
    assert m._movie_universe_membership(owned) == {200: {"mcu"}, 100: {"mcu"}}   # grouping, no Kometa tag
    assert m._movie_universe_order({}, owned) == {200: 0, 100: 1}               # saga order from list


def test_universe_source_serves_last_good_on_fetch_failure(monkeypatch):
    import scripts.managers.services.plex.playlists.builder as B
    monkeypatch.setattr(B.mdblist_client, "list_items",
                        lambda key, defn: {"ok": False, "items": [], "error": "mdblist down"})
    cache = _Cache()
    cache.set(B._UNIVERSE_SRC_KEY, {                                   # a prior good fetch, now STALE
        "universes": {"mcu": {"timeline": True, "movies": [100], "shows": []}},
        "fetched": {"mcu": 1}})                                        # ancient ordinal → refresh tried
    m = _mgr(cache=cache, config=_on_with_list_and_key())
    src = m._universe_source()
    assert src["universes"]["mcu"]["movies"] == [100]                 # failed fetch kept LAST-GOOD


def test_tv_franchise_maps_from_curated_when_enabled():
    m = _mgr(config=_ON)
    owned = [{"series_id": 10, "series_title": "Chicago Fire"},
             {"series_id": 11, "series_title": "Chicago P.D."},
             {"series_id": 99, "series_title": "Bluey"}]                 # unmatched
    fran, timeline = m._tv_franchise_maps(owned)
    assert fran == {10: "one chicago", 11: "one chicago"}               # Bluey absent
    assert timeline == {10: 0, 11: 1}                                    # saga order


def test_refresh_synthetic_universes_writes_tvfran_and_curated_keeps_name():
    import scripts.managers.services.plex.playlists.builder as B
    cache = _Cache()
    cache.set(B._UNIVERSE_SRC_KEY, {                                     # a prior mdblist list + a STALE tvfran ghost
        "universes": {"mcu": {"timeline": True, "movies": [1], "shows": []},
                      "tvfran:ghost": {"timeline": True, "movies": [], "shows": [999], "items": []}},
        "fetched": {"mcu": 5, "__tvfran__": 1}})
    m = _mgr(cache=cache, config=_ON)
    owned = [
        {"series_id": 1, "series_title": "CSI: Crime Scene Investigation", "series_tvdb_id": 10},
        {"series_id": 2, "series_title": "CSI: Miami", "series_tvdb_id": 11},   # non-curated stem → synthetic survives
        {"series_id": 3, "series_title": "Chicago Fire", "series_tvdb_id": 20},
        {"series_id": 4, "series_title": "Chicago P.D.", "series_tvdb_id": 21},  # curated → keeps "one chicago"
    ]
    fran, _ = m._tv_franchise_maps(owned)

    # (1) the cache is regenerated: stale ghost stripped, mdblist universe + its TTL untouched,
    #     BOTH discovered families written (so retention + acquisition see them too)
    src = cache.get(B._UNIVERSE_SRC_KEY)
    assert "tvfran:ghost" not in src["universes"]                        # stale synthetic stripped
    assert src["universes"]["mcu"]["movies"] == [1] and src["fetched"]["mcu"] == 5
    assert src["universes"]["tvfran:csi"]["shows"] == [10, 11]
    assert src["universes"]["tvfran:csi"]["timeline"] is True            # so acquisition won't skip it
    assert [it["tvdb"] for it in src["universes"]["tvfran:csi"]["items"]] == [10, 11]
    assert src["universes"]["tvfran:chicago"]["shows"] == [20, 21]       # written even though curated names it

    # (2) playlist grouping: CSI under its synthetic key, Chicago keeps the curated label
    assert fran[1] == "tvfran:csi" and fran[2] == "tvfran:csi"
    assert fran[3] == "one chicago" and fran[4] == "one chicago"         # curated wins over tvfran:chicago


def test_refresh_synthetic_universes_noop_without_cache():
    m = _mgr(config=_ON)                                                 # global_cache None
    fran, _ = m._tv_franchise_maps([{"series_id": 1, "series_title": "CSI: Miami", "series_tvdb_id": 10},
                                    {"series_id": 2, "series_title": "CSI: NY", "series_tvdb_id": 11}])
    assert fran == {}                                                    # no cache → no synthetic source to group from


def test_tv_franchise_catalog_includes_config_overlay():
    # the plex.playlists.tv_franchises overlay is merged (and overrides any baked/generated file), and
    # an operator-added family is stamped tier 0 (curated — config is trusted, so it acquires first).
    m = _mgr(config={"plex": {"playlists": {"tv_franchises": {"testverse": {"shows": [900001, 900002]}}}}})
    assert m._tv_franchise_catalog()["testverse"] == {"shows": [900001, 900002], "tier": 0}


def test_config_franchise_feeds_synthetic_universe_entry():
    import scripts.managers.services.plex.playlists.builder as B
    cache = _Cache()
    cache.set(B._UNIVERSE_SRC_KEY, {"universes": {}, "fetched": {}})
    cfg = {"plex": {"playlists": {"universe_timeline": {"enabled": True},
                                  # a cross-named catalog franchise (fake tvdbs → isolated from real data)
                                  "tv_franchises": {"testverse": {"shows": [900001, 900002]}}}}}
    m = _mgr(cache=cache, config=cfg)
    # own only 900001; 900002 is the unowned sibling the catalog still lists for acquisition backfill
    m._tv_franchise_maps([{"series_id": 1, "series_title": "Owned One", "series_tvdb_id": 900001}])
    syn = cache.get(B._UNIVERSE_SRC_KEY)["universes"]["tvfran:testverse"]
    assert syn["shows"] == [900001, 900002] and syn["timeline"] is True
    assert [it["tvdb"] for it in syn["items"]] == [900001, 900002]


def test_universe_source_restamps_timeline_flag_from_config():
    # the timeline flag is config-authoritative: a cached entry's stale timeline:True is overridden by
    # the CURRENT universe_lists() defn, so flipping a reverse-sorted list (fast/rocky) to False orders
    # it by release date on the NEXT run — without waiting for the 7-day TTL re-fetch.
    import scripts.managers.services.plex.playlists.builder as B
    cache = _Cache()
    cache.set(B._UNIVERSE_SRC_KEY, {
        "universes": {"fast": {"timeline": True, "movies": [1, 2], "shows": [],   # stale: fetched as True
                               "items": [{"media": "movie", "tmdb": 1, "rank": 0},
                                         {"media": "movie", "tmdb": 2, "rank": 1}]}},
        "fetched": {"fast": 9_999_999}})                                  # fresh → NOT re-fetched
    src = _mgr(cache=cache, config=_ON)._universe_source()                # no mdblist key; re-stamp still applies
    assert src["universes"]["fast"]["timeline"] is False                 # corrected from UNIVERSE_LISTS (fast=False)


def test_universe_timeline_bake_leads_full_interleave_into_cache():
    # the chronolists bake LEADS: its full movie+show order replaces the movies-only mdblist entry in the
    # cache (for the cache-reading consumers — retention + acquisition), and a new mdblist film tops up.
    # Driven here by the config overlay (self-contained, no file dependency).
    import scripts.managers.services.plex.playlists.builder as B
    cache = _Cache()
    cache.set(B._UNIVERSE_SRC_KEY, {
        "universes": {"dcu": {"timeline": True, "movies": [10, 99], "shows": [],
                              "items": [{"media": "movie", "tmdb": 10, "rank": 0},
                                        {"media": "movie", "tmdb": 99, "rank": 1}],
                              "titles": {"movie:99": "Brand New"}}},     # 99 = a film not yet in the bake
        "fetched": {"dcu": 5}})
    cfg = {"plex": {"playlists": {"universe_timeline": {"enabled": True, "universes": {
        "dcu": {"items": [{"media": "movie", "tmdb": 10, "title": "The Suicide Squad"},
                          {"media": "show", "tvdb": 700, "title": "Peacemaker"}]}}}}}}
    m = _mgr(cache=cache, config=cfg)
    m._tv_franchise_maps([])                                          # triggers _refresh_synthetic_universes
    dcu = cache.get(B._UNIVERSE_SRC_KEY)["universes"]["dcu"]
    assert [(it["media"], it.get("tmdb", it.get("tvdb"))) for it in dcu["items"]] == [
        ("movie", 10), ("show", 700), ("movie", 99)]                 # baked order, then the new film topped up
    assert dcu["shows"] == [700] and dcu["titles"]["show:700"] == "Peacemaker"
    assert dcu["items"][-1].get("src") == "mdblist"                  # 99 flagged as the mdblist top-up


def test_watchlisted_franchise_feeds_synthetic_universe_entry():
    import scripts.managers.services.plex.playlists.builder as B
    cache = _Cache()
    cache.set(B._UNIVERSE_SRC_KEY, {"universes": {}, "fetched": {}})
    cache.set("plex/watchlist/union", [{"type": "show", "ids": {"tvdb": 900003}}])   # watchlisted, UNOWNED
    cfg = {"plex": {"playlists": {"universe_timeline": {"enabled": True},
                                  "tv_franchises": {"wlverse": {"shows": [900003, 900004]}}}}}
    m = _mgr(cache=cache, config=cfg)
    # own NOTHING of wlverse (only an unrelated series); the watchlist add alone pulls the family in
    m._tv_franchise_maps([{"series_id": 1, "series_title": "Unrelated", "series_tvdb_id": 900099}])
    syn = cache.get(B._UNIVERSE_SRC_KEY)["universes"]["tvfran:wlverse"]
    assert syn["shows"] == [900003, 900004] and syn["timeline"] is True   # incl. the not-yet-owned siblings


def test_film_universe_deny_prevents_double_grouping():
    import scripts.managers.services.plex.playlists.builder as B
    cache = _Cache()
    # an mdblist FILM universe already groups show 900010 (e.g. Arrowverse)
    cache.set(B._UNIVERSE_SRC_KEY, {"universes": {"arrow": {"timeline": True, "movies": [],
                                    "shows": [900010], "items": []}}, "fetched": {"arrow": 5}})
    cfg = {"plex": {"playlists": {"universe_timeline": {"enabled": True},
                                  "tv_franchises": {"arrowtv": {"shows": [900010, 900011]}}}}}
    m = _mgr(cache=cache, config=cfg)
    m._tv_franchise_maps([{"series_id": 1, "series_title": "Arrow", "series_tvdb_id": 900010}])
    u = cache.get(B._UNIVERSE_SRC_KEY)["universes"]
    assert "arrow" in u                                                  # film universe preserved
    assert "tvfran:arrowtv" not in u                                     # catalog dup denied (film universe covers it)


def test_baked_floor_one_chicago_supersedes_stem_cluster():
    import scripts.managers.services.plex.playlists.builder as B
    cache = _Cache()
    cache.set(B._UNIVERSE_SRC_KEY, {"universes": {}, "fetched": {}})
    m = _mgr(cache=cache, config=_ON)
    # own 2 Chicago shows (real baked-floor tvdbs) → the floor's full "one chicago" supersedes the
    # owned-only stem cluster, so retention/acquisition see all 4 members (incl. unowned Med/Justice)
    m._tv_franchise_maps([{"series_id": 1, "series_title": "Chicago Fire", "series_tvdb_id": 258541},
                          {"series_id": 2, "series_title": "Chicago P.D.", "series_tvdb_id": 269641}])
    u = cache.get(B._UNIVERSE_SRC_KEY)["universes"]
    assert "tvfran:one chicago" in u and "tvfran:chicago" not in u
    assert u["tvfran:one chicago"]["shows"] == [258541, 269641, 295640, 311896]   # full floor membership


def test_builds_movie_plan_ranked_by_score():
    cache = _Cache()
    owned = [_movie(1, "Low", 2000, 20), _movie(2, "High", 2010, 90)]
    inv = {"1": {"rating_key": "lo", "title": "Low", "year": 2000},
           "2": {"rating_key": "hi", "title": "High", "year": 2010}}
    res = _mgr(cache)._build_for_users(_TRACKED, owned, inv, {"rob": set()}, {"rob": {}})
    assert res == {"users": 1, "built": 1, "can_build": True}
    assert _items(cache, "rob") == ["hi", "lo"]               # higher household score leads


def test_publishes_protected_tmdbs_for_the_delete_shield():
    # The movie builder must publish the union of recommended movie tmdbIds so the space
    # coordinator can shield them from deletion (don't delete what we recommend).
    cache = _Cache()
    owned = [_movie(7, "A", 2000, 80), _movie(8, "B", 2010, 90)]
    inv = {"7": {"rating_key": "a"}, "8": {"rating_key": "b"}}
    _mgr(cache)._build_for_users(_TRACKED, owned, inv, {"rob": set()}, {"rob": {}})
    assert cache.get(_PROTECTED_KEY) == {"tmdbs": [7, 8]}     # both planned movies, by tmdb_id


def test_protected_tmdbs_exclude_age_gated_movies():
    # A kid's plan excludes the R-rated movie, so its tmdb must NOT be published as protected.
    cache = _Cache()
    owned = [_movie(7, "Kids", 2000, 50, cert="G"), _movie(8, "Adult", 2010, 90, cert="R")]
    inv = {"7": {"rating_key": "k"}, "8": {"rating_key": "r"}}
    tracked = [{"safe_user": "wyatt", "title": "Wyatt", "restriction_profile": "little_kid"}]
    _mgr(cache)._build_for_users(tracked, owned, inv, {"wyatt": set()}, {"wyatt": {}})
    assert cache.get(_PROTECTED_KEY) == {"tmdbs": [7]}        # only the kid-safe movie is recommended


def test_no_inventory_short_circuits_with_actionable_warn():
    cache = _Cache()
    m = _mgr(cache)
    res = m._build_for_users(_TRACKED, [_movie(1, "X", 2000, 50)], {}, {"rob": set()}, {"rob": {}})
    assert res["can_build"] is False and res["built"] == 0
    assert any("plex.movies.enabled" in w for w in m.logger.warns)


def test_watched_movie_dropped():
    cache = _Cache()
    owned = [_movie(1, "Seen", 2000, 90), _movie(2, "Unseen", 2010, 50)]
    inv = {"1": {"rating_key": "a"}, "2": {"rating_key": "b"}}
    _mgr(cache)._build_for_users(_TRACKED, owned, inv, {"rob": {"a"}}, {"rob": {}})  # watched 'a'
    assert _items(cache, "rob") == ["b"]


def test_age_gate_excludes_adult_cert_for_kid():
    cache = _Cache()
    owned = [_movie(1, "Kids", 2000, 50, cert="G"), _movie(2, "Adult", 2010, 90, cert="R")]
    inv = {"1": {"rating_key": "k"}, "2": {"rating_key": "r"}}
    tracked = [{"safe_user": "wyatt", "title": "Wyatt", "restriction_profile": "little_kid"}]
    _mgr(cache)._build_for_users(tracked, owned, inv, {"wyatt": set()}, {"wyatt": {}})
    assert _items(cache, "wyatt") == ["k"]                    # R-rated excluded for a kid


def test_csm_age_fallback_admits_uncertified_movie_for_kid():
    # An uncertified movie (no MPAA cert) is fail-closed for a kid UNLESS a CSM age vouches.
    cache = _Cache()
    owned = [_movie(1, "NoCert Kid", 2000, 50, cert=None),     # CSM 5 → little-kid OK
             _movie(2, "NoCert Adult", 2010, 90, cert=None),   # CSM 17 → excluded
             _movie(3, "NoCert NoAge", 2015, 70, cert=None)]   # no age → fail-closed
    inv = {"1": {"rating_key": "a"}, "2": {"rating_key": "b"}, "3": {"rating_key": "c"}}
    tracked = [{"safe_user": "wyatt", "title": "Wyatt", "restriction_profile": "little_kid"}]
    _mgr(cache)._build_for_users(tracked, owned, inv, {"wyatt": set()}, {"wyatt": {}},
                                 csm_ages={1: 5, 2: 17})
    assert _items(cache, "wyatt") == ["a"]                     # only the CSM-young movie survives


def test_affinity_tilts_movie_order():
    cache = _Cache()
    owned = [_movie(1, "Doc", 2000, 90, genres='["Documentary"]'),
             _movie(2, "Boom", 2010, 60, genres='["Action"]')]
    inv = {"1": {"rating_key": "doc"}, "2": {"rating_key": "boom"}}
    m = _mgr(cache, config={"plex": {"playlists": {"personal_tilt": 90}}})
    m._build_for_users(_TRACKED, owned, inv, {"rob": set()}, {"rob": {"action": 100}})
    assert _items(cache, "rob") == ["boom", "doc"]            # affinity flips the household order


def test_nan_watchability_score_dropped_and_hh_max_safe():
    """REGRESSION (review): an un-scored movie reads back as float NaN (not None). It must
    NOT poison hh_max (max returns NaN when NaN is the first element) NOR float to a constant
    0.1 above real low-scored movies — it should drop to last."""
    cache = _Cache()
    # 'AAA' is alphabetically first (movie_files saves sorted by title) AND un-scored (NaN)
    owned = [_movie(1, "AAA", 2000, float("nan")), _movie(2, "Scored", 2010, 50)]
    inv = {"1": {"rating_key": "a"}, "2": {"rating_key": "b"}}
    _mgr(cache)._build_for_users(_TRACKED, owned, inv, {"rob": set()}, {"rob": {}})
    items = _items(cache, "rob")
    assert items[0] == "b"                                    # scored movie ranks (hh_max not NaN)
    assert items[-1] == "a"                                   # un-scored dropped to last, not 0.1


# ── Fresh Arrivals (opt-in second plan) ─────────────────────────────────────────
def _movie_added(tmdb, title, year, score, added_at, cert=None):
    return {"tmdb_id": tmdb, "title": title, "year": year, "watchability_score": score,
            "certification": cert, "in_cinemas_date": f"{year}-01-01", "added_at": added_at}


def test_fresh_arrivals_built_only_when_enabled_and_filtered_to_recent():
    # Dates relative to today so the builder's internal date.today() agrees with the test.
    recent = (date.today() - timedelta(days=5)).isoformat()
    stale = (date.today() - timedelta(days=100)).isoformat()
    owned = [_movie_added(1, "Fresh", 2000, 50, recent),
             _movie_added(2, "OldAcq", 2010, 90, stale)]      # high score but acquired long ago
    inv = {"1": {"rating_key": "fr"}, "2": {"rating_key": "ol"}}

    # default-OFF → no fresh plan cached; the up_next plan is unaffected.
    c0 = _Cache()
    _mgr(c0)._build_for_users(_TRACKED, owned, inv, {"rob": set()}, {"rob": {}})
    assert c0.get(f"{_FRESH_PLAN_KEY}/rob") is None
    assert c0.get(f"{_PLAN_KEY}/rob") is not None

    # enabled → a SECOND 'fresh' plan holding ONLY the recently-acquired movie (the high-scored
    # old acquisition is excluded — freshness is the acquisition date, not the score).
    c1 = _Cache()
    cfg = {"plex": {"playlists": {"fresh_arrivals": {"enabled": True, "acquired_window_days": 45}}}}
    _mgr(c1, config=cfg)._build_for_users(_TRACKED, owned, inv, {"rob": set()}, {"rob": {}})
    fresh = c1.get(f"{_FRESH_PLAN_KEY}/rob")
    assert fresh is not None and fresh["family"] == "fresh"
    assert [it["rating_key"] for it in fresh["items"]] == ["fr"]
    assert c1.get(f"{_PLAN_KEY}/rob") is not None             # up_next still built alongside


def test_fresh_arrivals_picks_join_the_delete_shield():
    recent = (date.today() - timedelta(days=3)).isoformat()
    owned = [_movie_added(7, "Fresh", 2000, 80, recent)]
    inv = {"7": {"rating_key": "a"}}
    cache = _Cache()
    cfg = {"plex": {"playlists": {"fresh_arrivals": {"enabled": True}}}}
    _mgr(cache, config=cfg)._build_for_users(_TRACKED, owned, inv, {"rob": set()}, {"rob": {}})
    assert cache.get(_PROTECTED_KEY) == {"tmdbs": [7]}        # recommended in fresh → shielded too
