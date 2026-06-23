"""Tests for the read-only anniversary-shelf builder: gather → score-once → per-user gate → cache two
plans + a household preview. The Sonarr/mdblist/Tautulli gathers and ``now`` are stubbed so the run is
deterministic; the REAL AcquisitionScorer runs against an empty cache."""
from __future__ import annotations

from datetime import datetime

from scripts.managers.services.plex.discovery import (
    _PREVIEW_KEY,
    DiscoveryShelfBuilderManager,
)
from scripts.managers.services.plex.playlists.writeback import (
    _TWIH_MOVIE_PLAN_KEY,
    _TWIH_SHOW_PLAN_KEY,
)

_NOW = datetime(2024, 12, 31)                          # week Dec 29 2024 – Jan 4 2025


class _Cache:
    def __init__(self, d=None): self.d = dict(d or {})
    def get(self, k, default=None): return self.d.get(k, default)
    def set(self, k, v): self.d[k] = v


class _Log:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_error(self, *a, **k): pass
    def log_table(self, *a, **k): pass


class _UsersMgr:
    def __init__(self, tracked, allowed): self.tracked_users = tracked; self._allowed = allowed
    def allowed_sections(self, u): return self._allowed.get(u["safe_user"], set())


class _Registry:
    def __init__(self, m): self.m = m
    def get(self, category, name): return self.m.get(name)


_MOVIES = [
    {"tmdbId": 1, "title": "Anniversary Movie", "inCinemas": "2010-01-02T00:00:00Z",
     "year": 2010, "genres": ["Action"], "hasFile": True, "certification": "PG-13",
     "ratings": {"tmdb": {"votes": 500, "value": 7.5}}},
    {"tmdbId": 2, "title": "Out Of Window", "inCinemas": "2010-06-01T00:00:00Z",
     "year": 2010, "hasFile": True, "ratings": {"tmdb": {"votes": 800}}},
    {"tmdbId": 3, "title": "Net New", "inCinemas": "2009-12-31T00:00:00Z",
     "year": 2009, "genres": ["Drama"], "hasFile": False,        # unowned → net-new preview only
     "ratings": {"tmdb": {"votes": 300}}},
]
_OWNED_EPS = [
    {"series_tvdb_id": 10, "series_id": 100, "series_title": "Anniversary Show",
     "season_number": 1, "episode_number": 1, "air_date_utc": "2010-01-02T00:00:00Z", "has_file": True},
]


def _mgr(*, tracked, allowed, config, cache=None):
    c = cache or _Cache({
        "radarr.movies.radarr.full": _MOVIES,
        "plex/movies/owned_inventory": {"1": {"rating_key": "rk1"}},
        "plex/episodes/owned_inventory": {"10:1:1": {"rating_key": "ep1"}},
        "plex/sections": {"1": {"type": "movie"}, "2": {"type": "show"}},
    })
    m = object.__new__(DiscoveryShelfBuilderManager)
    m.logger = _Log(); m.global_cache = c; m.config = config; m.plex_api = None; m.dry_run = True
    m.registry = _Registry({"PlexUsersManager": _UsersMgr(tracked, allowed)})
    m._household_now = lambda cfg: _NOW
    m._load_owned_episodes = lambda: list(_OWNED_EPS)
    m._series_certs = lambda: {}
    m._series_csm_ages = lambda: {}
    m._movie_csm_ages = lambda: {}
    return m, c


_ON = {"plex": {"playlists": {"this_week_in_history": {"enabled": True}}}}
_ROB = {"safe_user": "rob", "title": "Rob", "is_admin": True, "restriction_profile": None}


def test_disabled_is_byte_identical_noop():
    m, c = _mgr(tracked=[_ROB], allowed={"rob": {"1", "2"}},
                config={"plex": {"playlists": {}}})
    assert m.run() == {"enabled": False}
    assert not any(k.startswith("plex/playlists/twih") or k == _PREVIEW_KEY for k in c.d)


def test_builds_owned_movie_and_show_plans_for_opted_in_user():
    m, c = _mgr(tracked=[_ROB], allowed={"rob": {"1", "2"}}, config=_ON)
    stats = m.run()
    assert stats["built"] == 1
    movie_plan = c.d[f"{_TWIH_MOVIE_PLAN_KEY}/rob"]
    show_plan = c.d[f"{_TWIH_SHOW_PLAN_KEY}/rob"]
    assert [i["rating_key"] for i in movie_plan["items"]] == ["rk1"]   # owned anniversary movie
    assert [i["rating_key"] for i in show_plan["items"]] == ["ep1"]    # owned show via pilot
    preview = c.d[_PREVIEW_KEY]
    assert preview["owned_movies"] == 1 and preview["net_new_movies"] == 1   # tmdb 3 is net-new
    assert {row["tmdb_id"] for row in preview["movies"]} == {3}


def test_opt_in_list_excludes_unlisted_user():
    cfg = {"plex": {"playlists": {"this_week_in_history": {
        "enabled": True, "opt_in_users": ["Someone Else"]}}}}
    m, c = _mgr(tracked=[_ROB], allowed={"rob": {"1", "2"}}, config=cfg)
    assert m.run() == {"enabled": True, "users": 0, "built": 0}
    assert f"{_TWIH_MOVIE_PLAN_KEY}/rob" not in c.d


def test_library_gate_empties_plans_for_a_user_without_access():
    m, c = _mgr(tracked=[_ROB], allowed={"rob": set()}, config=_ON)   # no allowed sections
    m.run()
    assert c.d[f"{_TWIH_MOVIE_PLAN_KEY}/rob"]["items"] == []          # nothing → writeback tears down
    assert c.d[f"{_TWIH_SHOW_PLAN_KEY}/rob"]["items"] == []


def test_partial_library_grant_yields_no_movie_shelf_fail_closed():
    cache = _Cache({
        "radarr.movies.radarr.full": _MOVIES,
        "plex/movies/owned_inventory": {"1": {"rating_key": "rk1"}},
        "plex/episodes/owned_inventory": {"10:1:1": {"rating_key": "ep1"}},
        "plex/sections": {"1": {"type": "movie"}, "2": {"type": "movie"}, "3": {"type": "show"}},
    })
    # Rob is granted movie section 1 (not 2) + show section 3. Owned inventory has no per-item
    # section, so a PARTIAL movie grant fails closed → no movie shelf; the fully-granted show medium ok.
    m, c = _mgr(tracked=[_ROB], allowed={"rob": {"1", "3"}}, config=_ON, cache=cache)
    m.run()
    assert c.d[f"{_TWIH_MOVIE_PLAN_KEY}/rob"]["items"] == []               # movie_keys {1,2} not all granted
    assert [i["rating_key"] for i in c.d[f"{_TWIH_SHOW_PLAN_KEY}/rob"]["items"]] == ["ep1"]


def test_save_detection_records_watchlisted_anniversary_titles():
    cache = _Cache({
        "radarr.movies.radarr.full": _MOVIES,
        "plex/movies/owned_inventory": {"1": {"rating_key": "rk1"}},
        "plex/episodes/owned_inventory": {"10:1:1": {"rating_key": "ep1"}},
        "plex/sections": {"1": {"type": "movie"}, "2": {"type": "show"}},
        "plex/watchlist/union": [{"ids": {"tmdb": 3}}, {"ids": {"tvdb": 10}}],
    })
    m, c = _mgr(tracked=[_ROB], allowed={"rob": {"1", "2"}}, config=_ON, cache=cache)
    m.run()
    saved = c.d["discovery/saved/rob"]                # isolated key — never the affinity model
    assert saved["tmdb:3"]["media"] == "movie" and saved["tmdb:3"]["source"] == "watchlist"
    assert saved["tvdb:10"]["media"] == "show"
    assert "tautulli/affinity" not in c.d and "people_matrix/affinity" not in c.d


def test_two_users_get_taste_ordered_movie_shelves():
    movies = [
        {"tmdbId": 1, "title": "Comedy Hit", "inCinemas": "2010-01-02T00:00:00Z", "year": 2010,
         "genres": ["Comedy"], "hasFile": True, "certification": "PG", "ratings": {"tmdb": {"votes": 900}}},
        {"tmdbId": 4, "title": "Action Flick", "inCinemas": "2010-01-03T00:00:00Z", "year": 2010,
         "genres": ["Action"], "hasFile": True, "certification": "PG", "ratings": {"tmdb": {"votes": 500}}},
    ]
    cache = _Cache({
        "radarr.movies.radarr.full": movies,
        "plex/movies/owned_inventory": {"1": {"rating_key": "rkC"}, "4": {"rating_key": "rkA"}},
        "plex/episodes/owned_inventory": {},
        "plex/sections": {"1": {"type": "movie"}},
        "tautulli/users/rob/affinity": {"genres": {"action": 1.0}},   # Rob loves Action
        "tautulli/users/sam/affinity": {"genres": {"comedy": 1.0}},   # Sam loves Comedy
    })
    rob = {**_ROB, "tautulli_username": "rob"}
    sam = {"safe_user": "sam", "title": "Sam", "is_admin": True, "restriction_profile": None,
           "tautulli_username": "sam"}
    m, c = _mgr(tracked=[rob, sam], allowed={"rob": {"1"}, "sam": {"1"}}, config=_ON, cache=cache)
    m.run()
    rob_order = [i["rating_key"] for i in c.d[f"{_TWIH_MOVIE_PLAN_KEY}/rob"]["items"]]
    sam_order = [i["rating_key"] for i in c.d[f"{_TWIH_MOVIE_PLAN_KEY}/sam"]["items"]]
    assert rob_order[0] == "rkA"        # action lover → Action first (despite Comedy's higher votes)
    assert sam_order[0] == "rkC"        # comedy lover → Comedy first
    assert rob_order != sam_order       # individualized per user


def test_restricted_profile_age_gates_out_pg13_movie():
    kid = {"safe_user": "kid", "title": "Kid", "is_admin": False, "restriction_profile": "little_kid"}
    m, c = _mgr(tracked=[kid], allowed={"kid": {"1", "2"}}, config=_ON)
    m.run()
    # the only owned anniversary movie is PG-13 → excluded for a little kid (fail-closed).
    assert c.d[f"{_TWIH_MOVIE_PLAN_KEY}/kid"]["items"] == []
    # the show carries no cert (stubbed) → also excluded fail-closed for the kid.
    assert c.d[f"{_TWIH_SHOW_PLAN_KEY}/kid"]["items"] == []
