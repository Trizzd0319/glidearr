"""Tests that the acquisition Resolver honours the operator's `routing` preferences at add
time — but ONLY once the routing step has stamped routing.configured (a never-onboarded
install routes exactly as before). Covers the three add-time guards: movie kids-bucket off
collapses kids->standard; movie anime standard_only -> default instance + standard folder;
TV anime series_type -> series folder (seriesType stays anime); TV kids off -> series folder.
"""
from __future__ import annotations

from scripts.managers.services.acquisition.resolver import Resolver


class _Logger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_success(self, *a, **k): pass


class _GW:
    available = True

    def __init__(self, obj, cat=None):
        self._obj = obj
        self._cat = cat or {}

    def default_instance(self): return "standard"
    def categorized_instance(self, label="1080p"): return self._cat.get(label, "standard")
    def lookup(self, inst, term): return [self._obj]
    def in_library(self, inst, id_field, value): return False
    def quality_profiles(self, inst): return []
    def root_folders(self, inst): return [{"path": "/fallback"}]


_MRF = {"standard": "/m/std", "kids": "/m/kids", "anime": "/m/anime", "4k": "/m/4k"}
_RF = {"series": "/t/series", "anime": "/t/anime", "kids": "/t/kids"}


def _cfg(routing, configured=True):
    r = dict(routing)
    if configured:
        r["configured"] = True
    return {"routing": r, "movieRootFolders": dict(_MRF), "rootFolders": dict(_RF),
            "radarr_instances_categorized": {"anime": "animeinst"}}


def _movie(obj, cfg, age=None):
    r = Resolver({"radarr": _GW(obj, cat={"anime": "animeinst"})}, cfg, _Logger())
    r._movie_age_cache = {str(obj.get("tmdbId")): age} if age is not None else {}
    return r.prepare({"type": "movie", "ids": {"tmdb": obj["tmdbId"]}, "title": "X"})


def _show(obj, cfg, age=None):
    r = Resolver({"sonarr": _GW(obj)}, cfg, _Logger())
    r._show_age_cache = {str(obj.get("tmdbId")): age} if age is not None else {}
    return r.prepare({"type": "show", "ids": {"tvdb": obj["tvdbId"]}, "title": "X"})


# ── movie kids bucket ─────────────────────────────────────────────────────────
def test_movie_kids_routes_to_kids_folder_when_enabled():
    out = _movie({"tmdbId": 1, "genres": ["Drama"]},
                 _cfg({"movies": {"kids_bucket_enabled": True}, "tv": {}}), age=8)
    assert out["category"] == "kids" and out["root_folder"] == "/m/kids"


def test_movie_kids_collapses_to_standard_when_disabled():
    out = _movie({"tmdbId": 1, "genres": ["Drama"]},
                 _cfg({"movies": {"kids_bucket_enabled": False}, "tv": {}}), age=8)
    assert out["category"] == "kids"                       # still classified kids …
    assert out["root_folder"] == "/m/std"                  # … but routed to the standard folder


# ── movie anime instance + folder ─────────────────────────────────────────────
def test_movie_anime_dedicated_uses_anime_instance_and_folder():
    out = _movie({"tmdbId": 2, "genres": ["Animation"], "originalLanguage": {"name": "Japanese"}},
                 _cfg({"movies": {"anime_policy": "dedicated"}, "tv": {}}))
    assert out["category"] == "anime"
    assert out["instance"] == "animeinst" and out["root_folder"] == "/m/anime"


def test_movie_anime_standard_only_uses_default_instance_and_standard_folder():
    out = _movie({"tmdbId": 2, "genres": ["Animation"], "originalLanguage": {"name": "Japanese"}},
                 _cfg({"movies": {"anime_policy": "standard_only"}, "tv": {}}))
    assert out["category"] == "anime"                      # still classified anime …
    assert out["instance"] == "standard" and out["root_folder"] == "/m/std"   # … but default instance + standard


# ── TV anime folder (seriesType unaffected) ───────────────────────────────────
def test_tv_anime_plus_folder_uses_anime_folder():
    out = _show({"tvdbId": 10, "tmdbId": 10, "genres": ["Anime"]},
                _cfg({"movies": {}, "tv": {"anime_policy": "series_type_plus_folder"}}))
    assert out["category"] == "anime" and out["root_folder"] == "/t/anime"
    assert out["is_anime"] is True


def test_tv_anime_series_type_uses_series_folder_but_keeps_anime_parsing():
    out = _show({"tvdbId": 10, "tmdbId": 10, "genres": ["Anime"]},
                _cfg({"movies": {}, "tv": {"anime_policy": "series_type"}}))
    assert out["category"] == "anime" and out["root_folder"] == "/t/series"   # folder collapsed to series …
    assert out["is_anime"] is True                                            # … but still anime media (tag kept)


# ── TV kids bucket ────────────────────────────────────────────────────────────
def test_tv_kids_collapses_to_series_when_disabled():
    out = _show({"tvdbId": 11, "tmdbId": 11, "genres": ["Drama"]},
                _cfg({"movies": {}, "tv": {"kids_bucket_enabled": False}}), age=8)
    assert out["category"] == "kids" and out["root_folder"] == "/t/series"


# ── the stamp gate: prefs ignored until routing.configured ────────────────────
def test_prefs_ignored_without_configured_stamp():
    # kids_bucket_enabled False WOULD collapse to standard if configured — but no stamp means
    # the resolver routes exactly as before (uses the kids folder it found).
    out = _movie({"tmdbId": 1, "genres": ["Drama"]},
                 _cfg({"movies": {"kids_bucket_enabled": False}, "tv": {}}, configured=False), age=8)
    assert out["category"] == "kids" and out["root_folder"] == "/m/kids"
