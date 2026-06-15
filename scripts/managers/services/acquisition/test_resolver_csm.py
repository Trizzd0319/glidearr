"""Tests that the acquisition Resolver wires the Common Sense Media age cache into
add-time classification — the PRIMARY kids signal for BOTH movies and shows.

These are integration-ish: a real ``Resolver`` (its ``__init__`` only reads a config
dict, no network) with a fake gateway that returns one lookup ``obj``. The CSM cache is
injected directly onto the Resolver's lazy holder (``_movie_age_cache`` / ``_show_age_cache``)
so no on-disk cache file is read. This pins the full chain: the cache read, the
``isinstance(int)`` filter, the id used (``obj.get("tmdbId")`` for BOTH media), and that the
resolved ``category`` reflects CSM — plus that ``studio`` is still passed for the no-CSM fallback.
"""
from __future__ import annotations

from scripts.managers.services.acquisition.resolver import Resolver


class _Logger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_success(self, *a, **k): pass


class _GW:
    """Minimal ArrGateway stub: returns one lookup object and trivial folders/profiles.
    Empty quality_profiles() makes _pick_profile fall back to its hardcoded default, so the
    test never depends on size_model internals."""
    available = True

    def __init__(self, obj):
        self._obj = obj

    def default_instance(self): return "standard"
    def categorized_instance(self, label="1080p"): return "standard"
    def lookup(self, inst, term): return [self._obj]
    def in_library(self, inst, id_field, value): return False
    def quality_profiles(self, inst): return []
    def root_folders(self, inst): return [{"path": "/data"}]


def _movie_resolver(obj, age_cache_dict):
    r = Resolver({"radarr": _GW(obj)}, {}, _Logger())
    r._movie_age_cache = age_cache_dict      # inject — skips the lazy file load
    return r


def _show_resolver(obj, age_cache_dict):
    r = Resolver({"sonarr": _GW(obj)}, {}, _Logger())
    r._show_age_cache = age_cache_dict
    return r


# ── movies ──────────────────────────────────────────────────────────────────────
def test_movie_csm_kid_age_routes_kids():
    obj = {"tmdbId": 603, "genres": ["Drama"], "certification": "PG-13", "studio": "A24"}
    r = _movie_resolver(obj, {"603": 8})                       # CSM age 8 → kids (overrides cert/studio)
    out = r.prepare({"type": "movie", "ids": {"tmdb": 603}, "title": "X"})
    assert out["category"] == "kids"


def test_movie_csm_over_cutoff_not_kids():
    obj = {"tmdbId": 603, "genres": ["Family", "Comedy"], "certification": "G", "studio": "Walt Disney Pictures"}
    r = _movie_resolver(obj, {"603": 14})                      # CSM says 14 → NOT kids despite kids studio
    out = r.prepare({"type": "movie", "ids": {"tmdb": 603}, "title": "X"})
    assert out["category"] == "standard"


def test_movie_no_csm_uses_studio_fallback():
    # studio must reach the classifier: no CSM age + kids studio + kid-safe cert → kids.
    obj = {"tmdbId": 777, "genres": ["Comedy"], "certification": "G", "studio": "Pixar"}
    r = _movie_resolver(obj, {})                               # empty cache → no CSM age
    out = r.prepare({"type": "movie", "ids": {"tmdb": 777}, "title": "X"})
    assert out["category"] == "kids"


# ── shows (TV cache keyed by Sonarr series tmdbId, read off obj.tmdbId) ───────────
def test_show_csm_kid_age_routes_kids():
    obj = {"tvdbId": 121361, "tmdbId": 1399, "genres": ["Drama"], "certification": "TV-MA"}
    r = _show_resolver(obj, {"1399": 8})                       # CSM age 8 → kids (beats TV-MA drama)
    out = r.prepare({"type": "show", "ids": {"tvdb": 121361}, "title": "X"})
    assert out["category"] == "kids"


def test_show_csm_over_cutoff_blocks_kids():
    obj = {"tvdbId": 121361, "tmdbId": 1399, "genres": ["Drama", "Family"], "certification": "TV-PG"}
    r = _show_resolver(obj, {"1399": 16})                      # would be kids (soft family) without CSM
    out = r.prepare({"type": "show", "ids": {"tvdb": 121361}, "title": "X"})
    assert out["category"] == "series"
