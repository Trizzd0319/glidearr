"""Tests for PlexCollectionsManager.run() — per-section collection aggregation.

Collections are PER-SECTION on PMS: the global ``/library/collections`` endpoint (section_id=None)
returns nothing on a modern server, so the manager must iterate ``get_sections()`` and read each
section's collections (mirrors the playlist builder's ``_all_collections``)."""
from __future__ import annotations

from scripts.managers.services.plex.collections import (
    _COMPLETENESS_KEY,
    _INDEX_KEY,
    _MEMBERSHIP_KEY,
    PlexCollectionsManager,
)


class _Log:
    def __init__(self): self.infos = []
    def log_info(self, m): self.infos.append(m)
    def log_warning(self, m): pass
    def log_error(self, m): pass
    def log_debug(self, m): pass


class _Cache:
    def __init__(self): self.d = {}
    def get(self, k): return self.d.get(k)
    def set(self, k, v): self.d[k] = v


class _Meta:
    """Resolves a child item → external ids. Keyed by ratingKey for test simplicity (the real
    PlexMetadataManager keys off guid/Guid)."""
    def __init__(self, by_rk): self.by_rk = by_rk
    def resolve(self, guid, guids, rating_key=None, allow_network=False):
        return self.by_rk.get(rating_key, {})


class _Registry:
    def __init__(self, meta): self._meta = meta
    def get(self, kind, name):
        return self._meta if name == "PlexMetadataManager" else None


class _FakePlexAPI:
    """A Movies-section collection (with members) + a smart TV-section collection, spread across two
    sections. The global endpoint (section_id=None) returns EMPTY — only the per-section reads see them."""
    def get_sections(self):
        return {"MediaContainer": {"Metadata": [
            {"key": "1", "title": "Movies", "type": "movie"},
            {"key": "2", "title": "TV Shows", "type": "show"}]}}

    def get_collections(self, section_id=None):
        if section_id is None:
            return {"MediaContainer": {}}                       # modern server: global endpoint is empty
        if section_id == "1":
            return {"MediaContainer": {"Metadata": [
                {"ratingKey": "c1", "title": "Marvel", "childCount": "2"}]}}
        return {"MediaContainer": {"Metadata": [
            {"ratingKey": "c2", "title": "Star Trek", "smart": True, "content": "rule=opaque"}]}}

    def get_collection_children(self, rating_key):
        if rating_key == "c1":
            return {"MediaContainer": {"Metadata": [{"ratingKey": "m1"}, {"ratingKey": "m2"}]}}
        if rating_key == "c2":
            return {"MediaContainer": {"Metadata": [{"ratingKey": "s1"}]}}
        return {"MediaContainer": {"Metadata": []}}


def _mgr(cache, plex_api, meta=None):
    m = PlexCollectionsManager.__new__(PlexCollectionsManager)
    m.global_cache = cache
    m.logger = _Log()
    m.config = {}
    m.registry = _Registry(meta) if meta is not None else None
    m.plex_api = plex_api
    m.dry_run = False
    return m


def test_run_aggregates_collections_across_sections():
    cache = _Cache()
    meta = _Meta({"m1": {"tmdb": 603}, "m2": {"tmdb": 604}, "s1": {"tmdb": 1234}})
    m = _mgr(cache, _FakePlexAPI(), meta)

    res = m.run()
    assert res == {"collections": 2}                            # both sections' collections found

    index = cache.get(_INDEX_KEY)
    assert set(index) == {"c1", "c2"}
    assert index["c1"]["tmdb_members"] == [603, 604]
    assert index["c1"]["child_count"] == 2                      # numeric childCount honoured
    assert index["c1"]["smart"] is False and index["c1"]["smart_filter"] is None
    assert index["c2"]["smart"] is True                         # smart-rule grammar captured as opaque metadata
    assert index["c2"]["smart_filter"] == "rule=opaque"
    assert index["c2"]["child_count"] == 1                      # no childCount → falls back to resolved member count

    membership = cache.get(_MEMBERSHIP_KEY)
    assert membership == {"603": ["c1"], "604": ["c1"], "1234": ["c2"]}

    completeness = cache.get(_COMPLETENESS_KEY)
    assert completeness["c1"] == {"title": "Marvel", "have": 2, "child_count": 2}


def test_run_empty_when_no_sections():
    # A server with no sections (or the API soft-empties) yields no collections and never raises —
    # the manager degrades to empty caches, exactly as it should when nothing is reachable.
    class _NoSections:
        def get_sections(self): return {"MediaContainer": {}}
        def get_collections(self, section_id=None): return {"MediaContainer": {}}
        def get_collection_children(self, rating_key): return {"MediaContainer": {}}

    cache = _Cache()
    m = _mgr(cache, _NoSections())
    assert m.run() == {"collections": 0}
    assert cache.get(_INDEX_KEY) == {}
    assert cache.get(_MEMBERSHIP_KEY) == {}


def test_all_collections_empty_without_plex_api():
    m = _mgr(_Cache(), None)
    assert m._all_collections() == []
