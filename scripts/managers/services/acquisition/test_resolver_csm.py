"""Tests that the acquisition Resolver wires the Common Sense Media age cache into
add-time classification — a kids CEILING for BOTH movies and shows (operator policy:
"never trust Common Sense alone", so a low CSM age never routes to Kids by itself).

These are integration-ish: a real ``Resolver`` (its ``__init__`` only reads a config
dict, no network) with a fake gateway that returns one lookup ``obj``. The CSM cache is
injected directly onto the Resolver's lazy holder (``_movie_age_cache`` / ``_show_age_cache``)
so no on-disk cache file is read. This pins the full chain: the cache read, the
``isinstance(int)`` filter, the id used (``obj.get("tmdbId")`` for BOTH media), that a CSM age
over the cutoff DEMOTES out of Kids, and that the positive kids signals reach the classifier
(``studio`` for movies, ``network`` for shows).
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
def test_movie_csm_kid_age_alone_not_kids():
    # CSM age is a kids CEILING ONLY: a low CSM age with no kids studio does NOT route to Kids.
    obj = {"tmdbId": 603, "genres": ["Drama"], "certification": "PG-13", "studio": "A24"}
    r = _movie_resolver(obj, {"603": 8})
    out = r.prepare({"type": "movie", "ids": {"tmdb": 603}, "title": "X"})
    assert out["category"] == "standard"


def test_movie_kids_studio_in_csm_range_routes_kids():
    # The kids STUDIO is the positive signal; a CSM age within the cutoff does not block it.
    obj = {"tmdbId": 603, "genres": ["Comedy"], "certification": "G", "studio": "Pixar"}
    r = _movie_resolver(obj, {"603": 8})
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
def test_show_csm_kid_age_alone_not_kids():
    # CSM age alone no longer routes a show to Kids (Star Trek: DS9 pattern — adult drama, CSM ~10).
    obj = {"tvdbId": 121361, "tmdbId": 1399, "genres": ["Drama", "Science Fiction"],
           "certification": "TV-PG"}
    r = _show_resolver(obj, {"1399": 10})
    out = r.prepare({"type": "show", "ids": {"tvdb": 121361}, "title": "X"})
    assert out["category"] == "series"


def test_show_kids_network_reaches_classifier():
    # The show ``network`` reaches the classifier: a kids network routes to Kids even at a
    # CSM tween age and a non-kids cert (Star Trek: Prodigy on Nickelodeon, CSM ~10).
    obj = {"tvdbId": 121361, "tmdbId": 1399, "genres": ["Drama"],
           "certification": "TV-PG", "network": "Nickelodeon"}
    r = _show_resolver(obj, {"1399": 10})
    out = r.prepare({"type": "show", "ids": {"tvdb": 121361}, "title": "X"})
    assert out["category"] == "kids"


def test_show_csm_over_cutoff_blocks_kids():
    obj = {"tvdbId": 121361, "tmdbId": 1399, "genres": ["Drama", "Family"], "certification": "TV-PG"}
    r = _show_resolver(obj, {"1399": 16})                      # would be kids (soft family) without CSM
    out = r.prepare({"type": "show", "ids": {"tvdb": 121361}, "title": "X"})
    assert out["category"] == "series"
