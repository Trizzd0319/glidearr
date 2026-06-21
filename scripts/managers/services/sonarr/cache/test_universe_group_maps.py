"""SonarrCacheEpisodeFilesManager._universe_group_maps — the Sonarr-side accessor that builds the
group (franchise/universe) maps for the per-group prefetch walk from the series cache + the cached
universe source, gated by acquisition.universe.enabled (off → singleton groups → legacy walk)."""
from __future__ import annotations

import scripts.managers.services.sonarr.cache.episode_files as ef

_SRC = ef.SonarrCacheEpisodeFilesManager._UNIVERSE_SRC_KEY


class _SeriesCache:
    def __init__(self, rows): self._rows = rows
    def iter_all_series(self, instance): return self._rows


class _SonarrCache:
    def __init__(self, series_cache): self.series = series_cache


class _Cache:
    def __init__(self, d=None): self.d = d or {}
    def get(self, k): return self.d.get(k)


class _Logger:
    def log_info(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_debug(self, *a, **k): pass


def _mgr(*, enabled, rows, source, acq_enabled=True):
    m = object.__new__(ef.SonarrCacheEpisodeFilesManager)
    m.config = {"acquisition": {"enabled": acq_enabled, "universe": {"enabled": enabled}}}
    m.sonarr_cache = _SonarrCache(_SeriesCache(rows))
    m.global_cache = _Cache({_SRC: source} if source is not None else {})
    m.logger = _Logger()
    return m


def test_off_returns_empty_singletons():
    m = _mgr(enabled=False, rows=[{"id": 1, "title": "Chicago Fire", "tvdbId": None}], source=None)
    assert m._universe_group_maps("standard") == ({}, {})       # → walk uses per-series (byte-identical)


def test_parent_acquisition_off_disables_universe_even_if_child_on():
    # acquisition.universe is nested under acquisition: the master switch off disables the whole
    # subtree → byte-identical legacy walk, never running behind a switch the operator thinks is off.
    m = _mgr(enabled=True, acq_enabled=False,
             rows=[{"id": 1, "title": "Chicago Fire", "tvdbId": None},
                   {"id": 2, "title": "Chicago P.D.", "tvdbId": None}], source=None)
    assert m._universe_group_maps("standard") == ({}, {})


def test_groups_curated_and_universe_when_enabled():
    rows = [{"id": 1, "title": "Chicago Fire", "tvdbId": None},
            {"id": 2, "title": "Chicago P.D.", "tvdbId": None},
            {"id": 10, "title": "Loki", "tvdbId": 600},
            {"id": 99, "title": "Unrelated", "tvdbId": 700}]
    source = {"universes": {"mcu": {"timeline": True, "movies": [], "shows": [600]}}}
    fran, time = _mgr(enabled=True, rows=rows, source=source)._universe_group_maps("standard")
    assert fran == {1: "one chicago", 2: "one chicago", 10: "mcu"}   # 99 ungrouped
    assert time == {1: 0, 2: 1, 10: 0}                               # curated + timeline universe


def test_curated_groups_even_without_mdblist_source():
    # No universe source (no mdblist key) → curated TV franchises still group from the bundled map.
    rows = [{"id": 1, "title": "Chicago Fire", "tvdbId": None},
            {"id": 2, "title": "Chicago P.D.", "tvdbId": None}]
    fran, time = _mgr(enabled=True, rows=rows, source=None)._universe_group_maps("standard")
    assert fran == {1: "one chicago", 2: "one chicago"} and time == {1: 0, 2: 1}
