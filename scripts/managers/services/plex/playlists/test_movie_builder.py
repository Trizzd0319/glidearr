"""Tests for the per-user MOVIE playlist builder core (_build_for_users)."""
from __future__ import annotations

from scripts.managers.services.plex.playlists.movie_builder import (
    _PLAN_KEY,
    _PROTECTED_KEY,
    MoviePlaylistBuilderManager,
)


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
