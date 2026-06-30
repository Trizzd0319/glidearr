"""Tests for PlexMoviesManager — owned-movie tmdb→ratingKey scan + coverage probe."""
from __future__ import annotations

from scripts.managers.services.plex.movies import (
    _INVENTORY_KEY,
    _STATS_KEY,
    PlexMoviesManager,
)


class _Log:
    def log_info(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_error(self, *a, **k): pass


class _Cache:
    def __init__(self): self.d = {}
    def get(self, k): return self.d.get(k)
    def set(self, k, v): self.d[k] = v


class _Meta:
    """resolve guid → {tmdb} via a tiny ratingKey lookup."""
    def __init__(self, by_rk): self.by_rk = by_rk
    def resolve(self, guid, guids, rating_key=None, allow_network=False):
        return {"tmdb": self.by_rk.get(str(rating_key))}


class _Registry:
    def __init__(self, meta): self.meta = meta
    def get(self, kind, name): return self.meta if name == "PlexMetadataManager" else None


class _Api:
    """get_sections → one movie section; get_section_all → two movies on one page."""
    def get_sections(self):
        return {"MediaContainer": {"Directory": [{"key": "1", "title": "Movies", "type": "movie"}]}}

    def get_section_all(self, key, plex_type, start, size):
        if start > 0:
            return {"MediaContainer": {"Metadata": []}}
        return {"MediaContainer": {"totalSize": 2, "Metadata": [
            {"ratingKey": "5001", "title": "The Matrix", "year": 1999, "type": "movie"},
            {"ratingKey": "5002", "title": "Unmatched", "year": 2000, "type": "movie"},
        ]}}


def _mgr(cache, meta, api=None):
    m = PlexMoviesManager.__new__(PlexMoviesManager)
    m.logger = _Log()
    m.config = {}
    m.global_cache = cache
    m.registry = _Registry(meta)
    m.plex_api = api or _Api()
    m.dry_run = False
    return m


def test_scan_builds_tmdb_inventory_and_coverage():
    cache = _Cache()
    m = _mgr(cache, _Meta({"5001": 603}))      # only 5001 resolves to a tmdb
    stats = m.run()
    assert cache.get(_INVENTORY_KEY) == {
        "603": {"rating_key": "5001", "title": "The Matrix", "year": 1999, "section": "1"}}
    assert stats["movies_seen"] == 2 and stats["movies_resolved"] == 1
    assert stats["unresolved_no_tmdb"] == 1
    assert cache.get(_STATS_KEY)["resolution_pct"] == 50.0


def test_no_movie_sections_is_empty_not_error():
    cache = _Cache()

    class _NoMovies(_Api):
        def get_sections(self):
            return {"MediaContainer": {"Directory": [{"key": "2", "title": "TV", "type": "show"}]}}

    stats = _mgr(cache, _Meta({}), api=_NoMovies()).run()
    assert stats["movie_sections"] == 0 and stats["movies_seen"] == 0
    assert cache.get(_INVENTORY_KEY) == {}


# ── plex.exclude_sections: "Coming Soon" placeholder library is never scanned ────
def test_excluded_section_is_skipped():
    cache = _Cache()

    class _TwoSections(_Api):
        def get_sections(self):
            return {"MediaContainer": {"Directory": [
                {"key": "1", "title": "Movies", "type": "movie"},
                {"key": "9", "title": "Coming Soon", "type": "movie"},
            ]}}

        def get_section_all(self, key, plex_type, start, size):
            if start > 0:
                return {"MediaContainer": {"Metadata": []}}
            if str(key) == "1":
                return {"MediaContainer": {"totalSize": 1, "Metadata": [
                    {"ratingKey": "5001", "title": "The Matrix", "year": 1999, "type": "movie"}]}}
            return {"MediaContainer": {"totalSize": 1, "Metadata": [   # the unreleased placeholder
                {"ratingKey": "9001", "title": "Tron: Ares", "year": 2025, "type": "movie"}]}}

    m = _mgr(cache, _Meta({"5001": 603, "9001": 100000}), api=_TwoSections())
    m.config = {"plex": {"exclude_sections": ["Coming Soon"]}}
    stats = m.run()
    inv = cache.get(_INVENTORY_KEY)
    assert stats["movie_sections"] == 1
    assert inv == {"603": {"rating_key": "5001", "title": "The Matrix", "year": 1999, "section": "1"}}
    assert "100000" not in inv          # placeholder tmdb never registers as owned


def test_excluded_section_titles_helper():
    from scripts.managers.services.plex._common import excluded_section_titles
    assert excluded_section_titles(None) == set()
    assert excluded_section_titles({}) == set()
    assert excluded_section_titles({"plex": {}}) == set()
    assert excluded_section_titles({"plex": {"exclude_sections": []}}) == set()
    assert excluded_section_titles({"plex": {"exclude_sections": "Coming Soon"}}) == {"coming soon"}
    assert excluded_section_titles(
        {"plex": {"exclude_sections": [" Coming Soon ", "Coming Soon TV", "", "  "]}}
    ) == {"coming soon", "coming soon tv"}
