"""TraktPeopleMatrixManager — build from injected credits, cache + load roundtrip."""
from __future__ import annotations

from scripts.managers.services.trakt.people_matrix import TraktPeopleMatrixManager


class _Log:
    def log_info(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_debug(self, *a, **k): pass


class _GC:
    def __init__(self): self.d = {}
    def get(self, k): return self.d.get(k)
    def set(self, k, v): self.d[k] = v


def _mgr(tmp_path):
    m = TraktPeopleMatrixManager.__new__(TraktPeopleMatrixManager)
    m.logger, m.global_cache, m.dry_run, m.ttl = _Log(), _GC(), False, 999_999
    m.config = None
    m.matrix_path = tmp_path / "people_matrix.json.gz"
    m.affinity_path = tmp_path / "people_affinity.json.gz"
    m._movie_cache = m._show_cache = None
    return m


def _cast(*ids):
    return {"cast": [{"name": f"p{p}", "id": p, "order": i} for i, p in enumerate(ids)], "crew": []}


def test_build_caches_and_load_roundtrips(tmp_path):
    m = _mgr(tmp_path)
    media = {("movie", 24428): _cast(1245, 3223),
             ("movie", 271110): _cast(3223),
             ("show", 100): _cast(999)}
    stats = m.build(media_people=media)
    assert stats == {"titles": 3, "with_people": 3, "persons": 3, "weighted_people": 0}
    assert m.matrix_path.exists()

    pidx, fwd = m.load_index()                       # from global_cache
    assert pidx[3223] == {("movie", 24428), ("movie", 271110)}
    assert fwd[("show", 100)]["cast"] == [999]

    m.global_cache.d.clear()                          # force the gz fallback
    pidx2, _ = m.load_index()
    assert pidx2[1245] == {("movie", 24428)}


def test_build_empty_is_safe(tmp_path):
    m = _mgr(tmp_path)
    assert m.build(media_people={}) == {"titles": 0, "with_people": 0, "persons": 0,
                                        "weighted_people": 0}
    assert m.load_index() == (None, None)


def test_household_weights_from_cached_watched(tmp_path):
    m = _mgr(tmp_path)
    # household watched movie 24428 (Trakt history) → its cast gets affinity weight
    m.global_cache.d["trakt/history/movies"] = [{"movie": {"ids": {"tmdb": 24428}}}]
    media = {("movie", 24428): _cast(1245, 3223), ("movie", 271110): _cast(3223)}
    stats = m.build(media_people=media)
    assert stats["weighted_people"] == 2          # 1245 + 3223 each appear in the watched film
    aff = m.global_cache.d["people_matrix/affinity"]
    assert aff["1245"] == 1.0 and aff["3223"] == 1.0   # str keys in cache, cast weight 1.0


def test_load_index_missing_cache(tmp_path):
    m = _mgr(tmp_path)
    assert m.load_index() == (None, None)             # never built
