"""Regression test for item E: owned_episodes must be built once per run, not per builder.

The TV builder (gate=plex.episodes) and the combined builder (gate=plex.movies) both call
_load_owned_episodes, and SonarrCacheOwnedEpisodesManager.build_or_refresh re-iterates all
owned series + rewrites owned_episodes.parquet on every call (~15s each). The fix memoizes
the built rows on the shared in-memory cache (global_cache.memory), so the build runs once
per run and every sibling builder reuses it. With no global_cache the original always-build
behaviour is preserved.
"""
from __future__ import annotations

from scripts.managers.services.plex.playlists.builder import PlexPlaylistBuilderManager
from scripts.managers.services.plex.playlists.combined_builder import CombinedPlaylistBuilderManager
from scripts.managers.factories.cache.memory import MemoryManager


class _L:
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_info(self, *a, **k): pass


class _GC:
    """Stand-in global_cache exposing just the shared in-memory store the memo uses."""
    def __init__(self):
        self.memory = MemoryManager(logger=_L())


def _mk(cls, gc, counter):
    m = object.__new__(cls)                                   # skip __init__/registry/base
    m.global_cache = gc
    m._build_owned_episodes = lambda: (counter.append(1) or [{"series_id": 1}])  # type: ignore[attr-defined]
    return m


def test_built_once_across_sibling_builders():
    gc = _GC()
    counter: list = []
    tv = _mk(PlexPlaylistBuilderManager, gc, counter)
    assert tv._load_owned_episodes() == [{"series_id": 1}]
    assert tv._load_owned_episodes() == [{"series_id": 1}]    # same builder, second call
    # A DIFFERENT builder sharing the SAME global_cache (the combined builder).
    combined = _mk(CombinedPlaylistBuilderManager, gc, counter)
    assert combined._load_owned_episodes() == [{"series_id": 1}]
    assert counter == [1]                                     # built exactly once this run


def test_no_global_cache_preserves_always_build():
    counter: list = []
    m = object.__new__(PlexPlaylistBuilderManager)
    m.global_cache = None
    m._build_owned_episodes = lambda: (counter.append(1) or [])  # type: ignore[attr-defined]
    m._load_owned_episodes()
    m._load_owned_episodes()
    assert counter == [1, 1]                                  # no memo -> original behaviour
