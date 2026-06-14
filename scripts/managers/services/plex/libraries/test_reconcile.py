"""Tests for the P3 orphan/missing reconcile — pure set-diff, DIAGNOSTIC only.

orphans = in Plex ∖ *arr ; missing = in *arr ∖ Plex. Items with unresolved GUIDs
are simply absent from the Plex id-set (excluded), not false-positive orphans."""
from __future__ import annotations

from scripts.managers.services.plex.libraries import PlexLibrarySectionsManager


class _Logger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass


class _Cache:
    def __init__(self, d=None): self.d = dict(d or {})
    def get(self, k, default=None): return self.d.get(k, default)
    def set(self, k, v): self.d[k] = v


def _mgr(cache, arr):
    m = object.__new__(PlexLibrarySectionsManager)
    m.logger = _Logger(); m.global_cache = cache; m.config = {}
    m._arr_ids = lambda service, field: arr[service]
    return m


def test_reconcile_set_diff():
    cache = _Cache({"plex/library_ids": {
        "movie_tmdb": [1, 2, 3], "show_tvdb": [10, 20], "unresolved": 5}})
    arr = {"radarr": {2, 3, 4}, "sonarr": {20, 30}}
    out = _mgr(cache, arr).run_reconcile()
    assert out["orphans"]["movies_tmdb"] == [1]          # in Plex, not Radarr
    assert out["orphans"]["shows_tvdb"] == [10]
    assert out["missing"]["movies_tmdb"] == [4]          # in Radarr, not Plex
    assert out["missing"]["shows_tvdb"] == [30]
    assert cache.get("plex/reconcile/orphans") == out["orphans"]
    assert cache.get("plex/reconcile/missing") == out["missing"]


def test_reconcile_noop_without_library_ids():
    # no plex/library_ids cached (reconcile id-scan never enabled) → no diff
    assert _mgr(_Cache(), {"radarr": set(), "sonarr": set()}).run_reconcile() == {}


# ── inventory pass: section index always, id-scan when reconcile.enabled ───────
from scripts.managers.services.plex.metadata import PlexMetadataManager


def _meta():
    m = object.__new__(PlexMetadataManager)
    m.logger = _Logger(); m.global_cache = _Cache(); m.plex_api = None
    m._guid_map = {}; m._dirty = False; m._network_hops = 0; m._unresolved = {}
    m._imdb_to_tmdb = {}; m._tvdb_to_tmdb = {}; m._primed = True
    return m


class _Registry:
    def __init__(self, mgrs): self.m = dict(mgrs)
    def get(self, category, name): return self.m.get(name)


class _InvAPI:
    def get_sections(self, fallback=None):
        return {"MediaContainer": {"Directory": [
            {"key": "1", "type": "movie", "title": "Movies", "count": "2",
             "Location": [{"path": "/movies"}]},
            {"key": "2", "type": "show", "title": "TV", "count": "1"},
        ]}}
    def get_section_all(self, key, plex_type=None, start=0, size=200, fallback=None):
        if start > 0:
            return {"MediaContainer": {"totalSize": 0, "Metadata": []}}
        if key == "1":
            items = [{"ratingKey": "m1", "type": "movie", "guid": "plex://m1",
                      "Guid": [{"id": "tmdb://603"}]},
                     {"ratingKey": "m2", "type": "movie", "guid": "plex://m2", "Guid": []}]
        else:
            items = [{"ratingKey": "s1", "type": "show", "guid": "plex://s1",
                      "Guid": [{"id": "tvdb://121361"}]}]
        return {"MediaContainer": {"totalSize": len(items), "Metadata": items}}


def test_inventory_writes_sections_and_scans_ids_when_enabled():
    cache = _Cache()
    m = object.__new__(PlexLibrarySectionsManager)
    m.logger = _Logger(); m.global_cache = cache; m.plex_api = _InvAPI()
    m.config = {"plex": {"reconcile": {"enabled": True}}}
    m.registry = _Registry({"PlexMetadataManager": _meta()})
    m.run()
    sections = cache.get("plex/sections")
    assert sections["1"]["title"] == "Movies" and sections["1"]["locations"] == ["/movies"]
    libids = cache.get("plex/library_ids")
    assert libids["movie_tmdb"] == [603]      # m2 (no guid) → unresolved, excluded
    assert libids["show_tvdb"] == [121361]
    assert libids["unresolved"] == 1


def test_inventory_skips_idscan_when_reconcile_disabled():
    cache = _Cache()
    m = object.__new__(PlexLibrarySectionsManager)
    m.logger = _Logger(); m.global_cache = cache; m.plex_api = _InvAPI()
    m.config = {"plex": {}}                    # reconcile not enabled
    m.registry = _Registry({"PlexMetadataManager": _meta()})
    m.run()
    assert cache.get("plex/sections") is not None
    assert cache.get("plex/library_ids") is None   # heavy scan skipped
