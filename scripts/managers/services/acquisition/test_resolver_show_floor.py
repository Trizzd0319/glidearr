"""Resolver: SHOW adds are capped to the pilot 720 floor (pilot_interactive.floor_res), so the first
(pilot) grab lands <=720 and the watch-based upgrade path raises it later. Gated on floor_res>0 —
byte-identical when unset. Anime shows stay in the [Anime] family (never the live-action 720 profile)."""
from __future__ import annotations

from scripts.managers.services.acquisition.resolver import Resolver


class _Logger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_success(self, *a, **k): pass


def _sprofile(name, res):
    return {"id": res, "name": name,
            "items": [{"allowed": True, "quality": {"name": name, "resolution": res}}]}


# raw Sonarr order deliberately puts a 1080p-allowing profile FIRST (the old profiles[0] leak).
_SON = [_sprofile("HD-1080p", 1080), _sprofile("HD-720p", 720),
        _sprofile("Ultra-HD", 2160), _sprofile("[Anime] HD-1080p", 1080)]


class _GW:
    available = True

    def __init__(self, obj, profiles):
        self._obj = obj
        self._profiles = profiles

    def default_instance(self): return "standard"
    def categorized_instance(self, label="1080p"): return "standard"
    def lookup(self, inst, term): return [self._obj]
    def in_library(self, inst, id_field, value): return False
    def quality_profiles(self, inst): return self._profiles
    def root_folders(self, inst): return [{"path": "/tv"}]


def _show_resolver(floor_res, obj=None):
    obj = obj or {"tvdbId": 555, "title": "Foo", "genres": ["Drama"], "runtime": 45}
    cfg = {"pilot_interactive": {"floor_res": floor_res}, "rootFolders": {}, "movieRootFolders": {}}
    r = Resolver({"sonarr": _GW(obj, _SON)}, cfg, _Logger())
    r._show_age_cache = {}
    r._movie_age_cache = {}
    return r


def test_show_add_capped_to_720_floor_when_floor_set():
    r = _show_resolver(720)
    e = r.prepare({"type": "show", "ids": {"tvdb": 555}})
    assert e["quality_profile"]["name"] == "HD-720p"      # not the 1080p profiles[0]
    assert e["quality_profile"]["max_res"] == 720


def test_show_add_uncapped_when_floor_zero_is_byte_identical():
    r = _show_resolver(0)
    e = r.prepare({"type": "show", "ids": {"tvdb": 555}})
    assert e["quality_profile"]["name"] == _SON[0]["name"]  # old behaviour: first available profile


def test_anime_show_stays_in_anime_family_not_live_action_720():
    # No [Anime] <=720 profile exists yet, so an anime show floors to its family's LOWEST ([Anime]
    # HD-1080p) — never the live-action HD-720p (which bans x265 and would break anime grabbing).
    obj = {"tvdbId": 556, "title": "Anime", "genres": ["Animation"],
           "originalLanguage": {"name": "Japanese"}, "runtime": 24}
    r = _show_resolver(720, obj=obj)
    e = r.prepare({"type": "show", "ids": {"tvdb": 556}, "is_anime": True})
    assert e["quality_profile"]["name"].startswith("[Anime]")
