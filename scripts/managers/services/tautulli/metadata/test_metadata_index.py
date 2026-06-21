"""get_metadata_index_cached must INCREMENTALLY top up the cached index with newly-watched
rating_keys (so they resolve the SAME run) instead of waiting on the 7-day full-rebuild TTL —
without re-fetching keys it already has."""
from __future__ import annotations

from scripts.managers.services.tautulli.metadata import TautulliMetadataManager


class _Cache:
    def __init__(self, store=None):
        self.store = dict(store or {})
        self.sets: list = []

    def get(self, key, default=None):
        return self.store.get(key, default)

    def set(self, key, value):
        self.store[key] = value
        self.sets.append(key)

    def delete(self, key):
        self.store.pop(key, None)
        return True

    def get_or_generate_cache(self, key, generator_function, expiration_time=None,
                              regenerate_on_expiry=False, **kw):
        if key in self.store:                  # cache hit (within TTL): frozen snapshot
            return self.store[key]
        val = generator_function()             # miss/expired: full build
        self.store[key] = val
        return val


class _API:
    """Returns Tautulli-shaped get_metadata responses; records which keys were fetched."""
    def __init__(self, genres_by_rk):
        self.genres_by_rk = genres_by_rk
        self.calls: list = []

    def get_metadata(self, rating_key=None):
        self.calls.append(str(rating_key))
        g = self.genres_by_rk.get(str(rating_key))
        data = {} if g is None else {
            "genres": g, "media_info": [], "title": f"T{rating_key}",
            "year": 2000, "guids": [], "guid": "",
        }
        return {"response": {"result": "success", "data": data}}


class _Log:
    def log_info(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_debug(self, *a, **k): pass


def _mgr(cache, api):
    m = TautulliMetadataManager.__new__(TautulliMetadataManager)
    m.global_cache = cache
    m.tautulli_api = api
    m.logger = _Log()
    return m


def test_incremental_topup_fetches_only_missing_keys():
    # Cached index already covers '1' (with tmdb_id so it isn't schema-invalidated). '2'/'3' are
    # newly watched this run -> only those get fetched + merged; '1' is NOT re-fetched.
    cache = _Cache({"tautulli/metadata/index": {"1": {"genres": ["Drama"], "tmdb_id": 11}}})
    api = _API({"2": ["Comedy"], "3": ["Action"]})
    idx = _mgr(cache, api).get_metadata_index_cached(["1", "2", "3"])
    assert set(idx) == {"1", "2", "3"}
    assert idx["2"]["genres"] == ["Comedy"] and idx["3"]["genres"] == ["Action"]
    assert sorted(api.calls) == ["2", "3"]                 # '1' served from cache, not re-fetched
    assert "tautulli/metadata/index" in cache.sets         # merged index persisted for next run


def test_no_missing_keys_means_no_api_calls():
    cache = _Cache({"tautulli/metadata/index": {"1": {"genres": ["Drama"], "tmdb_id": 11}}})
    api = _API({})
    idx = _mgr(cache, api).get_metadata_index_cached(["1"])
    assert api.calls == []                                  # everything already cached
    assert idx == {"1": {"genres": ["Drama"], "tmdb_id": 11}}


def test_unresolvable_missing_key_does_not_crash_or_pollute():
    # A missing key Tautulli can't resolve (deleted from Plex -> empty data) is simply skipped.
    # With ZERO successes this run we do NOT negative-cache it (could be a down/keyless run).
    cache = _Cache({"tautulli/metadata/index": {"1": {"genres": ["Drama"], "tmdb_id": 11}}})
    api = _API({})                                          # '9' returns empty data
    idx = _mgr(cache, api).get_metadata_index_cached(["1", "9"])
    assert api.calls == ["9"] and "9" not in idx           # attempted, not stored
    assert "tautulli/metadata/unresolved" not in cache.store  # no poison on a total miss


def test_unresolvable_keys_are_negative_cached_and_skipped_next_run():
    # When the fetch yields a MIX (>=1 success proves the API is live), the dead keys are
    # remembered so future runs don't re-hit the API for ratingKeys that drifted / left Plex.
    cache = _Cache({"tautulli/metadata/index": {"1": {"genres": ["Drama"], "tmdb_id": 11}}})
    api = _API({"2": ["Comedy"]})                          # '2' resolves; '9' is dead
    m = _mgr(cache, api)
    m.get_metadata_index_cached(["1", "2", "9"])
    assert "2" in cache.store["tautulli/metadata/index"]
    assert cache.store.get("tautulli/metadata/unresolved") == ["9"]    # dead key remembered
    api.calls.clear()
    m.get_metadata_index_cached(["1", "2", "9"])           # next run
    assert api.calls == []                                 # '2' cached, '9' negative-cached → no hits
