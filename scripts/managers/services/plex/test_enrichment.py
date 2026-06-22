"""Tests for the P2/P4 enrichment fetchers (on_deck, ratings, collections,
playlists). Each: fetch → parse → GUID-resolve → cache, with the documented
default-off / dedup / PII behaviours."""
from __future__ import annotations

from scripts.managers.services.plex.collections import PlexCollectionsManager
from scripts.managers.services.plex.metadata import PlexMetadataManager
from scripts.managers.services.plex.on_deck import PlexOnDeckManager
from scripts.managers.services.plex.playlists import PlexPlaylistsManager
from scripts.managers.services.plex.ratings import PlexRatingsManager


class _Logger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass


class _Cache:
    def __init__(self, d=None): self.d = dict(d or {})
    def get(self, k, default=None): return self.d.get(k, default)
    def set(self, k, v): self.d[k] = v


def _meta():
    m = object.__new__(PlexMetadataManager)
    m.logger = _Logger(); m.global_cache = _Cache(); m.plex_api = None
    m._guid_map = {}; m._dirty = False; m._network_hops = 0; m._unresolved = {}
    m._imdb_to_tmdb = {}; m._tvdb_to_tmdb = {}; m._primed = True
    return m


class _Registry:
    """Sibling lookup by class name — exactly how the fetchers resolve metadata/users."""
    def __init__(self, mgrs): self.m = dict(mgrs)
    def get(self, category, name): return self.m.get(name)


def _wire(cls, api, cache, tokens, tracked=None, config=None):
    m = object.__new__(cls)
    m.logger = _Logger(); m.global_cache = cache; m.plex_api = api
    m.dry_run = False; m.user_tokens = tokens; m.config = config or {}
    m.registry = _Registry({
        "PlexMetadataManager": _meta(),
        "PlexUsersManager": type("U", (), {"tracked_users": tracked or []})(),
    })
    return m


# ── on_deck ────────────────────────────────────────────────────────────────────
class _OnDeckAPI:
    def get_on_deck(self, token=None, fallback=None):
        return {"MediaContainer": {"Metadata": [
            {"ratingKey": "e1", "type": "episode", "guid": "plex://e1",
             "Guid": [{"id": "tvdb://100"}], "viewOffset": 300000, "duration": 600000}]}}


def test_on_deck_builds_resume_fraction_and_union():
    cache = _Cache()
    tracked = [{"safe_user": "Rob", "title": "Rob"}]
    m = _wire(PlexOnDeckManager, _OnDeckAPI(), cache, {"Rob": "tA"}, tracked)
    stats = m.run()
    union = cache.get("plex/on_deck/union")
    assert stats["items"] == 1
    assert union[0]["resume_fraction"] == 0.5 and union[0]["user"] == "Rob"
    assert union[0]["ids"]["tvdb"] == 100
    assert cache.get("plex/users/Rob/on_deck")[0]["id"] == "e1"
    # no unbounded per-run snapshot (only the rolling union key)
    assert not any(k.startswith("plex/on_deck/snapshot/") for k in cache.d)


# ── ratings (per-member userRating, normalized to int) ──────────────────────────
class _RatingsAPI:
    def get_sections(self, fallback=None):
        return {"MediaContainer": {"Directory": [{"key": "1", "type": "movie"}]}}
    def get_section_all(self, key, plex_type=None, start=0, size=200, token=None,
                        extra_params=None, fallback=None):
        if start > 0:
            return {"MediaContainer": {"totalSize": 0, "Metadata": []}}
        return {"MediaContainer": {"totalSize": 2, "Metadata": [
            {"ratingKey": "m1", "type": "movie", "guid": "plex://m1",
             "Guid": [{"id": "tmdb://603"}], "userRating": 8.0},
            {"ratingKey": "m2", "type": "movie", "guid": "plex://m2",
             "Guid": [{"id": "tmdb://604"}], "userRating": 0}]}}   # 0 → dropped


def test_ratings_keeps_nonzero_normalized_to_int():
    cache = _Cache()
    tracked = [{"safe_user": "Rob", "title": "Rob"}]
    m = _wire(PlexRatingsManager, _RatingsAPI(), cache, {"Rob": "tA"}, tracked)
    m.run()
    rated = cache.get("plex/users/Rob/ratings")
    assert rated == {"603": 8}          # tmdb-keyed, int; the 0-rating dropped


# ── collections (membership_by_tmdb; smart filter captured opaque) ─────────────
class _CollectionsAPI:
    def get_sections(self, fallback=None):                       # collections are read PER SECTION
        return {"MediaContainer": {"Directory": [{"key": "1", "type": "movie"}]}}
    def get_collections(self, section_id=None, fallback=None):
        return {"MediaContainer": {"Metadata": [
            {"ratingKey": "c1", "title": "Marvel", "smart": True, "childCount": "2",
             "content": "genre=action"}]}}
    def get_collection_children(self, rating_key, fallback=None):
        return {"MediaContainer": {"Metadata": [
            {"ratingKey": "x", "type": "movie", "guid": "plex://x", "Guid": [{"id": "tmdb://1"}]},
            {"ratingKey": "y", "type": "movie", "guid": "plex://y", "Guid": [{"id": "tmdb://2"}]}]}}


def test_collections_membership_and_opaque_smart_filter():
    cache = _Cache()
    m = _wire(PlexCollectionsManager, _CollectionsAPI(), cache, {})
    m.run()
    idx = cache.get("plex/collections/index")
    assert idx["c1"]["smart"] and idx["c1"]["smart_filter"] == "genre=action"  # captured, not executed
    assert sorted(idx["c1"]["tmdb_members"]) == [1, 2]
    membership = cache.get("plex/collections/membership_by_tmdb")
    assert membership == {"1": ["c1"], "2": ["c1"]}


# ── playlists (video-only, deduped vs watchlist union) ─────────────────────────
class _PlaylistsAPI:
    def get_playlists(self, fallback=None):
        return {"MediaContainer": {"Metadata": [
            {"ratingKey": "p1", "title": "Mix", "playlistType": "video"},
            {"ratingKey": "p2", "title": "Songs", "playlistType": "audio"}]}}   # audio dropped
    def get_playlist_items(self, rating_key, fallback=None):
        return {"MediaContainer": {"Metadata": [
            {"ratingKey": "a", "type": "movie", "guid": "plex://a", "Guid": [{"id": "tmdb://5"}]},
            {"ratingKey": "b", "type": "movie", "guid": "plex://b", "Guid": [{"id": "tmdb://9"}]}]}}


def test_playlists_video_only_and_dedup_vs_watchlist():
    cache = _Cache({"plex/watchlist/union": [{"ids": {"tmdb": 9}}]})   # tmdb 9 already wanted
    m = _wire(PlexPlaylistsManager, _PlaylistsAPI(), cache, {})
    m.run()
    idx = cache.get("plex/playlists/index")
    assert set(idx.keys()) == {"p1"}                       # audio playlist excluded
    items = idx["p1"]["items"]
    assert [it["ids"]["tmdb"] for it in items] == [5]      # tmdb 9 deduped vs watchlist
