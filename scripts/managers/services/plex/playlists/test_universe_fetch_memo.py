"""Run-scoped EXTERNAL-fetch memo for the universe/collection helpers (shared builder base).

The TV, MOVIE and COMBINED playlist builders are three separate INSTANCES of the same base,
handed the SAME global_cache. Each of them independently walked the operator's Plex collections
(section listing + one children read per collection) and re-hit mdblist for stale universe lists,
so the identical EXTERNAL reads happened 2-4x per run for zero new information.

The fix memoizes ONLY the external fetch on ``global_cache.memory`` (fresh per run), and leaves
every LOCAL computation on top of it per-call. These tests pin all three halves of that contract:

  (a) the fetch happens ONCE across two builder instances sharing a global_cache, and both still
      produce their own correct filtered output,
  (b) the LOCAL computation is NOT shared — two builders with DIFFERENT owned sets get DIFFERENT
      franchise/order maps out of the SAME cached fetch,
  (c) memo ISOLATION — a fresh run (new global_cache) refetches, and a cache with no in-memory
      store falls back to the original always-fetch behaviour.
"""
from __future__ import annotations

import pytest

from scripts.managers.factories.cache.memory import MemoryManager
from scripts.managers.services.plex.playlists.builder import (
    PlexPlaylistBuilderManager,
    log_run_fetch_stats,
    run_fetch_stats,
)
from scripts.managers.services.plex.playlists.combined_builder import CombinedPlaylistBuilderManager
from scripts.managers.services.plex.playlists.movie_builder import MoviePlaylistBuilderManager


class _Log:
    def __init__(self): self.infos = []
    def log_info(self, m): self.infos.append(m)
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_error(self, *a, **k): pass


class _GC:
    """global_cache stand-in: a dict key/value store PLUS the shared in-memory ``MemoryManager``
    the run-scoped fetch memo lives on. One _GC == one run (mirrors GlobalCacheManager, which
    builds a fresh MemoryManager in __init__). ``memory=False`` models a cache with no in-memory
    store at all → the memo must degrade to always-fetch."""
    def __init__(self, memory=True):
        self.d = {}
        self.memory = MemoryManager(logger=_Log()) if memory else None

    def get(self, k): return self.d.get(k)
    def set(self, k, v): self.d[k] = v


class _CountingAPI:
    """A Plex server with a MOVIE section and a SHOW section, each holding one universe collection
    (Marvel Cinematic Universe). Every external read is counted so the de-duplication is provable.
    Children come back IN COLLECTION ORDER (Captain Marvel before Iron Man; Loki before WandaVision)."""
    _MOVIE_KIDS = [{"ratingKey": "cm"}, {"ratingKey": "iron"}]
    _SHOW_KIDS = [{"ratingKey": "loki", "Guid": [{"id": "tvdb://400000"}]},
                  {"ratingKey": "wanda", "Guid": [{"id": "tvdb://400001"}]}]

    def __init__(self):
        self.sections = 0
        self.collections = 0
        self.children: list = []                       # (ratingKey, include_guids) per live fetch

    def get_sections(self):
        self.sections += 1
        return {"MediaContainer": {"Metadata": [
            {"key": "1", "title": "Movies", "type": "movie"},
            {"key": "2", "title": "TV Shows", "type": "show"}]}}

    def get_collections(self, section_id=None):
        self.collections += 1
        rk = "mv" if str(section_id) == "1" else "tv"
        return {"MediaContainer": {"Metadata": [{"ratingKey": rk, "title": "Marvel Cinematic Universe"}]}}

    def get_collection_children(self, rk, include_guids=False):
        self.children.append((rk, include_guids))
        kids = self._MOVIE_KIDS if rk == "mv" else self._SHOW_KIDS
        return {"MediaContainer": {"Metadata": kids}}


_MCU_SOURCE = {"universes": {"mcu": {"timeline": True, "movies": [604, 603], "shows": []}}}


def _mgr(cls, gc, api, *, source=None):
    """A builder INSTANCE wired to the shared global_cache + Plex API, with the file-backed
    catalogs neutralised so each test fully controls the universe/franchise inputs."""
    m = cls.__new__(cls)
    m.global_cache = gc
    m.logger = _Log()
    m.config = {"plex": {"playlists": {"universe_timeline": {"enabled": True}}}}
    m.registry = None
    m.dry_run = False
    m.plex_api = api
    m._universe_source = lambda: (source if source is not None else _MCU_SOURCE)
    m._tv_franchise_catalog = lambda: {}
    m._universe_timeline_catalog = lambda: {}
    return m


# ── (a) ONE fetch across two builder instances, both still correct ──────────────────────
def test_collection_fetch_happens_once_across_two_builders_sharing_a_cache():
    gc, api = _GC(), _CountingAPI()
    inv = {"603": {"rating_key": "iron"}, "604": {"rating_key": "cm"}}
    owned = [{"tmdb_id": 603}, {"tmdb_id": 604}]

    movie = _mgr(MoviePlaylistBuilderManager, gc, api)
    first = movie._plex_collection_order(inv, owned)
    assert first == {604: 0, 603: 1}                        # collection order (saga), not release
    assert (api.sections, api.collections) == (1, 2)        # one walk: 1 section list + 2 sections
    assert api.children == [("mv", False), ("tv", False)]   # one children read per collection

    # A DIFFERENT builder instance sharing the SAME global_cache: same inputs → same output,
    # ZERO new external reads.
    combined = _mgr(CombinedPlaylistBuilderManager, gc, api)
    assert combined._plex_collection_order(inv, owned) == first
    assert (api.sections, api.collections) == (1, 2)
    assert api.children == [("mv", False), ("tv", False)]

    # …and the SHOW reader on a third instance shares the listing too (only the guid-bearing
    # children payloads are new, because that reader asks for a different payload shape).
    tv = _mgr(PlexPlaylistBuilderManager, gc, api)
    fran, timeline = tv._plex_tv_collection_order({400000: 11, 400001: 22})
    assert fran == {11: "mcu", 22: "mcu"} and timeline == {11: 0, 22: 1}
    assert (api.sections, api.collections) == (1, 2)                        # listing still shared
    assert api.children == [("mv", False), ("tv", False), ("mv", True), ("tv", True)]

    combined2 = _mgr(CombinedPlaylistBuilderManager, gc, api)
    assert combined2._plex_tv_collection_order({400000: 11, 400001: 22}) == (fran, timeline)
    assert len(api.children) == 4                                          # nothing refetched


# ── (b) the LOCAL computation is NOT shared ─────────────────────────────────────────────
def test_local_computation_not_shared_different_owned_sets_differ():
    """Two builders on ONE cached fetch, with DIFFERENT owned sets, must produce DIFFERENT maps —
    only the external payload is memoized, never the filtering/joining done on top of it."""
    gc, api = _GC(), _CountingAPI()

    # TV: builder A owns both MCU shows, builder B owns only WandaVision.
    a = _mgr(PlexPlaylistBuilderManager, gc, api)
    b = _mgr(CombinedPlaylistBuilderManager, gc, api)
    fran_a, time_a = a._plex_tv_collection_order({400000: 11, 400001: 22})
    fran_b, time_b = b._plex_tv_collection_order({400001: 99})
    assert fran_a == {11: "mcu", 22: "mcu"} and time_a == {11: 0, 22: 1}
    assert fran_b == {99: "mcu"} and time_b == {99: 0}     # own owned set → own (dense) map
    assert fran_a != fran_b
    assert api.children.count(("mv", True)) == 1 and api.children.count(("tv", True)) == 1

    # MOVIE: same collection payload, different owned inventory → different order map.
    both = _mgr(MoviePlaylistBuilderManager, gc, api)
    only_iron = _mgr(CombinedPlaylistBuilderManager, gc, api)
    assert both._plex_collection_order(
        {"603": {"rating_key": "iron"}, "604": {"rating_key": "cm"}},
        [{"tmdb_id": 603}, {"tmdb_id": 604}]) == {604: 0, 603: 1}
    assert only_iron._plex_collection_order(
        {"603": {"rating_key": "iron"}}, [{"tmdb_id": 603}]) == {603: 0}
    assert api.children.count(("mv", False)) == 1 and api.children.count(("tv", False)) == 1


# ── (c) memo isolation: a fresh run refetches; no memory store → always fetch ────────────
def test_fresh_run_refetches():
    api = _CountingAPI()
    inv = {"603": {"rating_key": "iron"}, "604": {"rating_key": "cm"}}
    owned = [{"tmdb_id": 603}, {"tmdb_id": 604}]

    run1 = _GC()
    _mgr(MoviePlaylistBuilderManager, run1, api)._plex_collection_order(inv, owned)
    _mgr(CombinedPlaylistBuilderManager, run1, api)._plex_collection_order(inv, owned)
    assert (api.sections, len(api.children)) == (1, 2)      # one run → one fetch

    run2 = _GC()                                            # NEW run == new global_cache/memory
    assert _mgr(MoviePlaylistBuilderManager, run2, api)._plex_collection_order(inv, owned) \
        == {604: 0, 603: 1}
    assert (api.sections, len(api.children)) == (2, 4)      # refetched, output unchanged


def test_no_memory_store_preserves_always_fetch():
    api = _CountingAPI()
    gc = _GC(memory=False)                                  # cache with no in-memory store
    inv = {"603": {"rating_key": "iron"}, "604": {"rating_key": "cm"}}
    owned = [{"tmdb_id": 603}, {"tmdb_id": 604}]
    m = _mgr(MoviePlaylistBuilderManager, gc, api)
    assert m._plex_collection_order(inv, owned) == {604: 0, 603: 1}
    assert m._plex_collection_order(inv, owned) == {604: 0, 603: 1}
    assert api.sections == 2 and len(api.children) == 4     # no memo → original behaviour


def test_no_global_cache_at_all_still_reads_live():
    api = _CountingAPI()
    m = _mgr(MoviePlaylistBuilderManager, None, api)
    assert m._plex_collection_order({"603": {"rating_key": "iron"}}, [{"tmdb_id": 603}]) == {603: 0}
    assert api.sections == 1


# ── mdblist: the HTTP fetch is memoized, the merge around it is not ──────────────────────
def test_mdblist_list_fetch_shared_across_builders():
    import scripts.managers.services.plex.playlists.builder as B
    gc = _GC()
    calls: list = []

    def _fake_list_items(key, defn):
        calls.append((key, defn.get("imdb") or defn.get("id") or defn.get("mdblist")))
        return {"ok": True, "items": [{"tmdb": 200, "tvdb": None, "media": "movie"}]}

    orig = B.mdblist_client.list_items
    B.mdblist_client.list_items = _fake_list_items
    try:
        defn = {"imdb": "ls123", "timeline": True}
        a = _mgr(PlexPlaylistBuilderManager, gc, _CountingAPI())
        b = _mgr(CombinedPlaylistBuilderManager, gc, _CountingAPI())
        res_a = a._fetch_universe_list("apikey", "mcu", defn)
        res_b = b._fetch_universe_list("apikey", "mcu", defn)
        assert res_a == res_b and res_a["ok"]
        assert calls == [("apikey", "ls123")]                    # ONE HTTP fetch this run
        # A DIFFERENT universe (and a re-pointed list) is a different fetch.
        b._fetch_universe_list("apikey", "starwars", {"imdb": "ls999"})
        a._fetch_universe_list("apikey", "mcu", {"imdb": "ls456"})
        assert len(calls) == 3
        # A fresh run (new global_cache → new memory store) refetches the very same list.
        _mgr(PlexPlaylistBuilderManager, _GC(), _CountingAPI())._fetch_universe_list(
            "apikey", "mcu", defn)
        assert len(calls) == 4
    finally:
        B.mdblist_client.list_items = orig


def test_universe_source_does_not_refetch_mdblist_for_a_sibling_builder(monkeypatch):
    """End-to-end through _universe_source: even when the TTL cache write doesn't stick (so both
    builders see the universe as STALE), the mdblist HTTP fetch runs once — while the merge/
    re-stamp around it stays per-call."""
    import scripts.managers.services.plex.playlists.builder as B
    calls: list = []
    monkeypatch.setattr(B.mdblist_client, "list_items", lambda key, defn: (
        calls.append(defn.get("id") or defn.get("imdb")),
        {"ok": True, "items": [{"tmdb": 100, "tvdb": None, "media": "movie"}]})[1])
    monkeypatch.setattr(B, "universe_lists", lambda cfg: {"mcu": {"imdb": "ls1", "timeline": True}})

    class _NoWriteGC(_GC):                      # the TTL stamp never lands → always "stale"
        def set(self, k, v): pass

    gc = _NoWriteGC()
    cfg = {"plex": {"playlists": {"universe_timeline": {"enabled": True}}}, "mdblist": {"apikey": "k"}}
    a = _mgr(PlexPlaylistBuilderManager, gc, _CountingAPI())
    b = _mgr(CombinedPlaylistBuilderManager, gc, _CountingAPI())
    for m in (a, b):
        m.config = cfg
        del m._universe_source                  # exercise the REAL implementation
    assert a._universe_source()["universes"]["mcu"]["movies"] == [100]
    assert b._universe_source()["universes"]["mcu"]["movies"] == [100]
    assert calls == ["ls1"]                     # one HTTP fetch across both builders


# ── instrumentation: the per-run fetch-count line ───────────────────────────────────────
def test_fetch_stats_counters_and_log_line():
    gc, api = _GC(), _CountingAPI()
    inv = {"603": {"rating_key": "iron"}, "604": {"rating_key": "cm"}}
    owned = [{"tmdb_id": 603}, {"tmdb_id": 604}]
    _mgr(MoviePlaylistBuilderManager, gc, api)._plex_collection_order(inv, owned)
    _mgr(CombinedPlaylistBuilderManager, gc, api)._plex_collection_order(inv, owned)

    stats = run_fetch_stats(gc)
    assert stats["collection_list_fetches"] == 1 and stats["collection_list_hits"] == 1
    assert stats["collection_children_fetches"] == 2 and stats["collection_children_hits"] == 2

    log = _Log()
    log_run_fetch_stats(log, gc)
    assert len(log.infos) == 1
    assert "external fetches this run" in log.infos[0]
    assert "Plex collection children 2 (+2 memo hit(s))" in log.infos[0]

    log2 = _Log()                               # nothing fetched → no line at all
    log_run_fetch_stats(log2, _GC())
    log_run_fetch_stats(log2, None)
    assert log2.infos == []


@pytest.mark.parametrize("cls", [PlexPlaylistBuilderManager, MoviePlaylistBuilderManager,
                                 CombinedPlaylistBuilderManager])
def test_all_three_builders_share_one_memo(cls):
    """Whichever builder gets there first pays for the walk; the other two ride free."""
    gc, api = _GC(), _CountingAPI()
    _mgr(cls, gc, api)._all_collections()
    for other in (PlexPlaylistBuilderManager, MoviePlaylistBuilderManager,
                  CombinedPlaylistBuilderManager):
        assert len(_mgr(other, gc, api)._all_collections()) == 2
    assert api.sections == 1 and api.collections == 2
