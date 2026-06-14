"""Tests for the flagship watchlist fetcher: per-user fetch → GUID-resolve → union
with attribution → bounded snapshots. Verifies dedup merges attribution, ids are
resolved before the union (so acquisition _dedup works), and a transient all-fail
preserves the prior good union (fail-closed)."""
from __future__ import annotations

from scripts.managers.services.plex.metadata import PlexMetadataManager
from scripts.managers.services.plex.watchlist import PlexWatchlistManager


class _Logger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass


class _Cache:
    def __init__(self, d=None): self.d = dict(d or {})
    def get(self, k, default=None): return self.d.get(k, default)
    def set(self, k, v): self.d[k] = v
    def delete(self, k): self.d.pop(k, None); return True


def _meta():
    m = object.__new__(PlexMetadataManager)
    m.logger = _Logger(); m.global_cache = _Cache(); m.plex_api = None
    m._guid_map = {}; m._dirty = False; m._network_hops = 0; m._unresolved = {}
    m._imdb_to_tmdb = {}; m._tvdb_to_tmdb = {}; m._primed = True
    return m


class _Registry:
    """Minimal stand-in for RegistryManager — sibling lookup by class name, exactly
    how the real fetchers resolve metadata/users."""
    def __init__(self, mgrs): self.m = dict(mgrs)
    def get(self, category, name): return self.m.get(name)


def _registry(tracked):
    return _Registry({
        "PlexMetadataManager": _meta(),
        "PlexUsersManager": type("U", (), {"tracked_users": tracked})(),
    })


class _API:
    """get_watchlist returns a single page keyed by token."""
    def __init__(self, by_token):
        self.by_token = by_token
    def get_watchlist(self, token, start=0, size=100, fallback=None):
        if start > 0:
            return {"MediaContainer": {"totalSize": 0, "Metadata": []}}
        items = self.by_token.get(token)
        if items is None:
            return None
        return {"MediaContainer": {"totalSize": len(items), "Metadata": items}}


def _wl(api, cache, tokens, tracked=()):
    m = object.__new__(PlexWatchlistManager)
    m.logger = _Logger(); m.global_cache = cache; m.plex_api = api
    m.dry_run = False; m.user_tokens = tokens; m.config = {}
    m.registry = _registry(list(tracked))
    return m


def _item(title, tmdb=None, tvdb=None, typ="movie", guid="plex://x"):
    guids = []
    if tmdb: guids.append({"id": f"tmdb://{tmdb}"})
    if tvdb: guids.append({"id": f"tvdb://{tvdb}"})
    return {"ratingKey": title, "title": title, "year": 2020, "type": typ, "guid": guid, "Guid": guids}


# ── union: dedup keys on resolved ids + merges attribution ────────────────────
def test_union_merges_attribution_across_users():
    tracked = [{"safe_user": "Rob", "title": "Rob"}, {"safe_user": "Kid", "title": "Kid"}]
    api = _API({
        "tA": [_item("Dune", tmdb=438631)],
        "tB": [_item("Dune", tmdb=438631), _item("Bluey", tvdb=355554, typ="show")],
    })
    cache = _Cache()
    m = _wl(api, cache, {"Rob": "tA", "Kid": "tB"}, tracked)
    stats = m.run()
    union = cache.get("plex/watchlist/union")
    assert stats["users"] == 2
    dune = next(u for u in union if u["title"] == "Dune")
    assert sorted(dune["watchlisted_by"]) == ["Kid", "Rob"]     # attribution merged
    assert dune["ids"]["tmdb"] == 438631                        # resolved before union
    assert dune["source"] == "plex_watchlist"
    assert len(union) == 2                                       # Dune deduped, Bluey distinct


def test_per_user_cache_written():
    tracked = [{"safe_user": "Rob", "title": "Rob"}]
    cache = _Cache()
    m = _wl(_API({"tA": [_item("Dune", tmdb=1)]}), cache, {"Rob": "tA"}, tracked)
    m.run()
    assert cache.get("plex/users/Rob/watchlist")[0]["title"] == "Dune"


# ── snapshot retention (rolling window) ───────────────────────────────────────
def test_snapshot_retention_prunes_oldest():
    tracked = [{"safe_user": "Rob", "title": "Rob"}]
    cache = _Cache()
    m = _wl(_API({"tA": [_item("Dune", tmdb=1)]}), cache, {"Rob": "tA"}, tracked)
    m.config = {"plex": {"watchlist": {"snapshot_retention": 2}}}
    for i in range(4):
        m._write_snapshot([{"title": "x"}], ts=f"20260101T00000{i}Z")
    idx = cache.get("plex/watchlist/snapshots_index")
    assert len(idx) == 2                                         # capped
    # only the 2 most-recent snapshot keys survive
    snap_keys = [k for k in cache.d if k.startswith("plex/watchlist/snapshot/")]
    assert len(snap_keys) == 2


# ── fail-closed: empty union with a prior good one preserves prior ────────────
def test_empty_union_preserves_prior_good():
    tracked = [{"safe_user": "Rob", "title": "Rob"}]
    cache = _Cache({"plex/watchlist/union": [{"title": "Old", "ids": {}}]})
    # token fetch returns None (transient) → no items → empty union
    m = _wl(_API({"tA": None}), cache, {"Rob": "tA"}, tracked)
    m.run()
    assert cache.get("plex/watchlist/union") == [{"title": "Old", "ids": {}}]  # preserved


# ── paging: must not truncate when Discover omits totalSize ───────────────────
class _PagedAPI:
    """Returns full pages with `size` but NO `totalSize` (the Discover schema variance
    that the old size-fallback truncated to one page)."""
    def get_watchlist(self, token, start=0, size=100, fallback=None):
        if start == 0:
            return {"MediaContainer": {"size": 100,
                                       "Metadata": [_item(f"M{i}", tmdb=i + 1, guid=f"plex://m{i}")
                                                    for i in range(100)]}}
        if start == 100:
            return {"MediaContainer": {"size": 5,
                                       "Metadata": [_item(f"M{i}", tmdb=i + 1, guid=f"plex://m{i}")
                                                    for i in range(100, 105)]}}
        return {"MediaContainer": {"size": 0, "Metadata": []}}


def test_paging_fetches_all_pages_when_totalsize_absent():
    cache = _Cache()
    m = _wl(_PagedAPI(), cache, {"Rob": "tA"}, [{"safe_user": "Rob", "title": "Rob"}])
    m.run()
    assert len(cache.get("plex/watchlist/union")) == 105   # all pages, not truncated to 100


# ── mid-page failure preserves the prior complete cache (fail-closed) ──────────
class _FailPage2API:
    def get_watchlist(self, token, start=0, size=100, fallback=None):
        if start == 0:
            return {"MediaContainer": {"size": 100,
                                       "Metadata": [_item(f"M{i}", tmdb=i) for i in range(100)]}}
        return None   # page 2 transient failure


def test_midpage_failure_preserves_prior_and_skips_truncated_write():
    cache = _Cache({"plex/watchlist/union": [{"title": "Prior", "ids": {}}],
                    "plex/users/Rob/watchlist": [{"title": "PriorRob"}]})
    m = _wl(_FailPage2API(), cache, {"Rob": "tA"}, [{"safe_user": "Rob", "title": "Rob"}])
    m.run()
    # page-2 None → whole fetch fails closed → per-user cache untouched, prior union kept
    assert cache.get("plex/users/Rob/watchlist") == [{"title": "PriorRob"}]
    assert cache.get("plex/watchlist/union") == [{"title": "Prior", "ids": {}}]


# ── acquisition feed reads the cached union ───────────────────────────────────
def test_acquisition_candidates_reads_union():
    cache = _Cache({"plex/watchlist/union": [{"title": "A", "ids": {"tmdb": 5}}]})
    m = _wl(_API({}), cache, {})
    assert m.acquisition_candidates() == [{"title": "A", "ids": {"tmdb": 5}}]
