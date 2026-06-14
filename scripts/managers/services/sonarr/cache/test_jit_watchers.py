"""Tests for _build_jit_watchers — per-series recent-watcher attribution that
annotates the JIT grab grid's 'For' column (display-only)."""
from __future__ import annotations

from scripts.managers.services.sonarr.cache.episode_files import (
    SonarrCacheEpisodeFilesManager,
)


class _Series:
    def __init__(self, rows):
        self._rows = rows

    def iter_all_series(self, instance):
        return self._rows


class _Cache:
    def __init__(self, series):
        self.series = series


def _mgr(series_rows):
    m = SonarrCacheEpisodeFilesManager.__new__(SonarrCacheEpisodeFilesManager)
    m.sonarr_cache = _Cache(_Series(series_rows))
    return m


_SERIES = [{"id": 1, "title": "Landman"}, {"id": 2, "title": "Bluey (2018)"}]


def test_orders_watchers_by_recency_per_series():
    m = _mgr(_SERIES)
    history = {
        ("Landman", 1, 1): {"per_user": {"Trizzd": "2026-06-10T00:00:00Z",
                                          "Guest": "2026-06-12T00:00:00Z"}},
        ("Landman", 1, 2): {"per_user": {"Trizzd": "2026-06-13T00:00:00Z"}},  # Trizzd later here
        ("Bluey (2018)", 1, 5): {"per_user": {"Aiden / Raina": "2026-06-11T00:00:00Z"}},
    }
    w = m._build_jit_watchers("sonarr", history)
    assert w["1"] == ["Trizzd", "Guest"]      # Trizzd's latest (E2) is most recent
    assert w["2"] == ["Aiden / Raina"]


def test_unmatched_series_and_empty_dropped():
    m = _mgr(_SERIES)
    history = {
        ("Unknown Show", 1, 1): {"per_user": {"X": "2026-06-12T00:00:00Z"}},  # no Sonarr match
        ("Landman", 1, 1): {"per_user": {}},                                  # no watchers
    }
    assert m._build_jit_watchers("sonarr", history) == {}


def test_none_timestamp_sorts_last_and_title_match_is_case_insensitive():
    m = _mgr([{"id": 7, "title": "The Rookie"}])
    history = {("the rookie", 1, 1): {"per_user": {"Dated": "2026-01-01T00:00:00Z",
                                                   "NoDate": None}}}
    assert m._build_jit_watchers("sonarr", history)["7"] == ["Dated", "NoDate"]


def test_no_series_cache_returns_empty():
    m = SonarrCacheEpisodeFilesManager.__new__(SonarrCacheEpisodeFilesManager)
    m.sonarr_cache = _Cache(None)
    assert m._build_jit_watchers("sonarr", {("X", 1, 1): {"per_user": {"a": "t"}}}) == {}
