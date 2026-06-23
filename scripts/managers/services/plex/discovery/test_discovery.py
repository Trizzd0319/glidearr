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
        "plex/movies/owned_inventory": {"1": {"rating_key": "rk1", "section": "1"}},
        "plex/episodes/owned_inventory": {"10:1:1": {"rating_key": "ep1", "section": "2"}},
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


def test_partial_movie_grant_scopes_to_granted_section():
    # Two owned anniversary movies in two different movie libraries; Rob is shared only "Kids Movies"
    # (section 1), not "Adult Movies" (section 2). He should get a SCOPED shelf — only the section-1
    # pick — not nothing (the old medium-level fail-closed) and not the un-shared section-2 pick.
    movies = [
        {"tmdbId": 1, "title": "Kids Pick", "inCinemas": "2010-01-02T00:00:00Z", "year": 2010,
         "genres": ["Family"], "hasFile": True, "certification": "PG", "ratings": {"tmdb": {"votes": 900}}},
        {"tmdbId": 2, "title": "Adult Pick", "inCinemas": "2010-01-03T00:00:00Z", "year": 2010,
         "genres": ["Action"], "hasFile": True, "certification": "PG", "ratings": {"tmdb": {"votes": 800}}},
    ]
    cache = _Cache({
        "radarr.movies.radarr.full": movies,
        "plex/movies/owned_inventory": {"1": {"rating_key": "rkKids", "section": "1"},
                                        "2": {"rating_key": "rkAdult", "section": "2"}},
        "plex/episodes/owned_inventory": {},
        "plex/sections": {"1": {"type": "movie"}, "2": {"type": "movie"}},
    })
    m, c = _mgr(tracked=[_ROB], allowed={"rob": {"1"}}, config=_ON, cache=cache)
    m.run()
    movie_rks = [i["rating_key"] for i in c.d[f"{_TWIH_MOVIE_PLAN_KEY}/rob"]["items"]]
    assert movie_rks == ["rkKids"]            # ONLY the granted section's pick; section 2 excluded


def test_owned_entry_without_section_excluded_fail_closed():
    # A legacy/partial inventory entry that carries no `section` can't prove the user was shared it →
    # excluded, even though the user holds a scoped grant for that medium (unknown section → fail-closed).
    cache = _Cache({
        "radarr.movies.radarr.full": _MOVIES,
        "plex/movies/owned_inventory": {"1": {"rating_key": "rk1"}},      # no `section`
        "plex/episodes/owned_inventory": {},
        "plex/sections": {"1": {"type": "movie"}, "2": {"type": "show"}},
    })
    m, c = _mgr(tracked=[_ROB], allowed={"rob": {"1", "2"}}, config=_ON, cache=cache)
    m.run()
    assert c.d[f"{_TWIH_MOVIE_PLAN_KEY}/rob"]["items"] == []


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
        "plex/movies/owned_inventory": {"1": {"rating_key": "rkC", "section": "1"},
                                        "4": {"rating_key": "rkA", "section": "1"}},
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


def test_owned_watched_movie_demoted_to_bottom():
    # An owned anniversary movie the viewer has FINISHED isn't excluded — it's demoted to the BOTTOM as
    # an anniversary rewatch. The unwatched one leads even though the watched one has a higher score.
    movies = [
        {"tmdbId": 1, "title": "Seen It", "inCinemas": "2010-01-02T00:00:00Z", "year": 2010,
         "genres": ["Action"], "hasFile": True, "certification": "PG", "ratings": {"tmdb": {"votes": 900}}},
        {"tmdbId": 4, "title": "Never Seen", "inCinemas": "2010-01-03T00:00:00Z", "year": 2010,
         "genres": ["Action"], "hasFile": True, "certification": "PG", "ratings": {"tmdb": {"votes": 500}}},
    ]
    cache = _Cache({
        "radarr.movies.radarr.full": movies,
        "plex/movies/owned_inventory": {"1": {"rating_key": "rkSeen", "section": "1"},
                                        "4": {"rating_key": "rkNew", "section": "1"}},
        "plex/episodes/owned_inventory": {},
        "plex/sections": {"1": {"type": "movie"}},
    })
    m, c = _mgr(tracked=[_ROB], allowed={"rob": {"1"}}, config=_ON, cache=cache)
    m._watched_movies_for = lambda uid: {"rkSeen"}        # Rob finished "Seen It"
    m.run()
    items = c.d[f"{_TWIH_MOVIE_PLAN_KEY}/rob"]["items"]
    assert [i["rating_key"] for i in items] == ["rkNew", "rkSeen"]   # unwatched first, rewatch at bottom
    assert items[0]["seen"] is False and items[1]["seen"] is True


def test_owned_watched_show_demoted_series_level():
    # SERIES-level: watching ANY owned episode demotes the whole show to the rewatch tier (marked seen).
    m, c = _mgr(tracked=[_ROB], allowed={"rob": {"1", "2"}}, config=_ON)   # default cache: tvdb 10 → ep1
    m._watched_for = lambda uid: {"ep1"}
    m.run()
    items = c.d[f"{_TWIH_SHOW_PLAN_KEY}/rob"]["items"]
    assert [i["rating_key"] for i in items] == ["ep1"] and items[0]["seen"] is True


def test_watched_movie_matched_by_title_year_after_rescan():
    # A Plex re-scan changed the ratingKey, so the watched set holds ONLY the (title,year) tuple — not
    # the current inventory ratingKey. The filter must STILL recognise the finished movie (mark it seen
    # → rewatch tier); a bare-ratingKey check would mis-classify it as unwatched on every re-scan.
    movies = [
        {"tmdbId": 1, "title": "Seen It", "inCinemas": "2010-01-02T00:00:00Z", "year": 2010,
         "genres": ["Action"], "hasFile": True, "certification": "PG", "ratings": {"tmdb": {"votes": 900}}},
    ]
    cache = _Cache({
        "radarr.movies.radarr.full": movies,
        "plex/movies/owned_inventory": {"1": {"rating_key": "NEW_RK", "section": "1"}},  # post-rescan rk
        "plex/episodes/owned_inventory": {},
        "plex/sections": {"1": {"type": "movie"}},
    })
    m, c = _mgr(tracked=[_ROB], allowed={"rob": {"1"}}, config=_ON, cache=cache)
    m._watched_movies_for = lambda uid: {("seen it", 2010)}   # only the surviving tuple identity
    m.run()
    items = c.d[f"{_TWIH_MOVIE_PLAN_KEY}/rob"]["items"]
    assert len(items) == 1 and items[0]["seen"] is True       # recognised via the tuple, not the rk


def test_watched_show_matched_by_series_tuple_after_rescan():
    # TV twin of the churn case: the watched set holds only the (series,season,episode) tuple, not the
    # post-rescan ratingKeys — the series must still be recognised as seen.
    owned_eps = [
        {"series_tvdb_id": 10, "series_id": 100, "series_title": "Show", "season_number": 3,
         "episode_number": 4, "air_date_utc": "2010-01-02T00:00:00Z", "has_file": True},
    ]
    cache = _Cache({
        "radarr.movies.radarr.full": [],
        "plex/movies/owned_inventory": {},
        "plex/episodes/owned_inventory": {
            "10:1:1": {"rating_key": "NEW_PILOT", "series_title": "Show", "title": "Pilot", "section": "2"},
            "10:3:4": {"rating_key": "NEW_ANNIV", "series_title": "Show", "title": "Anniversary", "section": "2"}},
        "plex/sections": {"2": {"type": "show"}},
    })
    m, c = _mgr(tracked=[_ROB], allowed={"rob": {"2"}}, config=_ON, cache=cache)
    m._load_owned_episodes = lambda: list(owned_eps)
    m._watched_for = lambda uid: {("show", 3, 4)}             # only the surviving (series,s,e) tuple
    m.run()
    items = c.d[f"{_TWIH_SHOW_PLAN_KEY}/rob"]["items"]
    assert len(items) == 1 and items[0]["seen"] is True


def test_watched_non_pilot_episode_demotes_the_series():
    # The gap episode-level filtering misses: Rob watched a MID-series episode (S3E4), NOT the pilot the
    # shelf surfaces. Series-level recognition must still mark the show seen (rewatch tier), not unwatched.
    owned_eps = [
        {"series_tvdb_id": 10, "series_id": 100, "series_title": "Show", "season_number": 3,
         "episode_number": 4, "air_date_utc": "2010-01-02T00:00:00Z", "has_file": True},
    ]
    cache = _Cache({
        "radarr.movies.radarr.full": [],
        "plex/movies/owned_inventory": {},
        "plex/episodes/owned_inventory": {
            "10:1:1": {"rating_key": "pilot10", "series_title": "Show", "section": "2"},   # surfaced pilot
            "10:3:4": {"rating_key": "anniv10", "series_title": "Show", "section": "2"}},   # the watched ep
        "plex/sections": {"2": {"type": "show"}},
    })
    m, c = _mgr(tracked=[_ROB], allowed={"rob": {"2"}}, config=_ON, cache=cache)
    m._load_owned_episodes = lambda: list(owned_eps)
    m._watched_for = lambda uid: {"anniv10"}             # watched S3E4 only — NOT the surfaced pilot
    m.run()
    items = c.d[f"{_TWIH_SHOW_PLAN_KEY}/rob"]["items"]
    assert len(items) == 1 and items[0]["seen"] is True   # series demoted despite the unwatched pilot


def test_no_tautulli_match_fails_open_and_shows_owned():
    # Default _mgr (no Tautulli managers in the registry) → empty watched set → owned still shown.
    m, c = _mgr(tracked=[_ROB], allowed={"rob": {"1", "2"}}, config=_ON)
    m.run()
    assert [i["rating_key"] for i in c.d[f"{_TWIH_MOVIE_PLAN_KEY}/rob"]["items"]] == ["rk1"]
