"""Regression test for items F.1/F.2: the full-Tautulli reseed fallback in
composite_sync_workflow must fire ONLY on a genuine cold start (no Sonarr history
bookmark yet), not on every quiet run.

A recent history bookmark + an empty since-window routinely yields 0 history records
(Sonarr was simply idle). Previously that empty result triggered a re-seed from the entire
ever-watched Tautulli catalogue every time — a heavy, mis-scoped pass. The fix gates the
fallback on the cold-start flag (timestamp age is None) captured before the history fetch
rewrites the bookmark.
"""
from __future__ import annotations

from scripts.managers.services.sonarr.series.sync import SonarrSeriesSyncManager


class _L:
    def log_info(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_debug(self, *a, **k): pass


class _WatchHistorySpy:
    """Spy on the Tautulli fallback: get_all_history_cached is only called if the heavy
    reseed path is entered."""
    def __init__(self): self.calls = 0
    def get_all_history_cached(self):
        self.calls += 1
        return []                      # -> 0 titles -> recent stays empty -> clean skip


class _TautulliSeries:
    def get_series_completion_stats(self, entries): return {}


class _TautulliMgr:
    def __init__(self, spy):
        self.series = _TautulliSeries()
        self.watch_history = spy


class _Registry:
    def __init__(self, tmgr): self._t = tmgr
    def get(self, kind, name): return self._t if name == "TautulliManager" else None


class _TimestampHandler:
    def __init__(self, age): self._age = age
    def get_age_seconds(self, *a, **k): return self._age


class _GC:
    def __init__(self, age): self.timestamp_handler = _TimestampHandler(age)
    def get(self, key): return None


class _IM:
    def resolve_instance(self, i): return i or "sonarr"


class _History:
    def get_recent_sonarr_series(self, inst): return set()   # empty window (0 records)


def _mk(age):
    spy = _WatchHistorySpy()
    m = object.__new__(SonarrSeriesSyncManager)              # skip __init__/registry/base
    m.logger = _L()
    m.dry_run = True
    m.instance_manager = _IM()
    m.history = _History()
    m.global_cache = _GC(age)
    m.registry = _Registry(_TautulliMgr(spy))
    return m, spy


def test_cold_start_triggers_reseed():
    # age None == no history bookmark == genuine first run -> reseed fires.
    m, spy = _mk(None)
    m.composite_sync_workflow("sonarr")
    assert spy.calls == 1


def test_idle_recent_bookmark_skips_reseed():
    # A recent bookmark (1h old) + 0 records == idle steady state -> NO reseed.
    m, spy = _mk(3600)
    m.composite_sync_workflow("sonarr")
    assert spy.calls == 0
