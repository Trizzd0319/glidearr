"""Tests for the owned-episode inventory — the separate, unpruned playlists feed.
Includes the CROWN-JEWEL guard: building it never targets episode_files.parquet.
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.services.sonarr.cache.owned_episodes import (
    COLUMNS,
    SonarrCacheOwnedEpisodesManager,
)

M = SonarrCacheOwnedEpisodesManager


def epobj(season, episode, has_file=True, file_id=None, monitored=True,
          air="2020-01-01T00:00:00Z", title="ep"):
    return {"seasonNumber": season, "episodeNumber": episode, "hasFile": has_file,
            "episodeFileId": file_id, "monitored": monitored, "airDateUtc": air,
            "title": title}


# ── _build_owned_rows (pure) ──────────────────────────────────────────────────
def test_only_owned_episodes_become_rows():
    meta = {1: {"tvdb": 555, "title": "Show"}}
    eps = {1: [epobj(1, 1, has_file=True, file_id=10),
               epobj(1, 2, has_file=False)]}            # not owned → excluded
    rows = M._build_owned_rows(meta, eps)
    assert len(rows) == 1 and rows[0]["episode_file_id"] == 10
    assert rows[0]["tvdb_join_key"] == "555:1:1"


def test_join_key_is_null_safe():
    # series with no tvdbId → join key None (counted, never guessed), build succeeds
    rows = M._build_owned_rows({1: {"tvdb": None, "title": "X"}}, {1: [epobj(1, 1)]})
    assert rows[0]["tvdb_join_key"] is None
    assert M._join_key(7, None, 3) is None and M._join_key(7, 1, 1) == "7:1:1"


def test_specials_flagged_and_keyed():
    rows = M._build_owned_rows({1: {"tvdb": 9, "title": "X"}}, {1: [epobj(0, 1)]})
    assert rows[0]["is_special"] is True and rows[0]["tvdb_join_key"] == "9:0:1"


def test_multi_ep_file_yields_distinct_keys_sharing_one_file_id():
    eps = {1: [epobj(1, 1, file_id=42), epobj(1, 2, file_id=42)]}   # one file, two eps
    rows = M._build_owned_rows({1: {"tvdb": 3, "title": "X"}}, eps)
    assert {r["tvdb_join_key"] for r in rows} == {"3:1:1", "3:1:2"}
    assert {r["episode_file_id"] for r in rows} == {42}


# ── build_or_refresh (mocked siblings) + the crown-jewel guard ─────────────────
class _FakeSeries:
    def iter_all_series(self, instance):
        return [{"id": 1, "tvdbId": 100, "title": "Alpha"},
                {"id": 2, "tvdbId": None, "title": "Beta"}]


class _FakeEpisodeFiles:
    def __init__(self):
        self.cleanup_called = False

    def _get_all_episodes(self, instance, sid):
        if sid == 1:
            return {1: [epobj(1, 2, file_id=2), epobj(1, 1, file_id=1)]}   # out of order
        return {1: [epobj(1, 1, file_id=9, has_file=False)]}              # Beta: unowned

    # if owned_episodes ever called the pruner, this would trip — it must not.
    def _do_cleanup_non_essential(self, *a, **k):
        self.cleanup_called = True


class _FakeCache:
    def __init__(self, base):
        class _KB:  # key_builder.base_dir
            pass
        self.key_builder = _KB()
        self.key_builder.base_dir = base


class _SonarrCache:
    def __init__(self, ep):
        self.series = _FakeSeries()
        self.episode_files = ep


def _mgr(tmp_path):
    m = M.__new__(M)
    m.logger = type("L", (), {"log_info": lambda *a, **k: None,
                              "log_warning": lambda *a, **k: None})()
    ep = _FakeEpisodeFiles()
    m.sonarr_cache = _SonarrCache(ep)
    m.global_cache = _FakeCache(tmp_path)
    m.instance_manager = None
    m.dry_run = True
    return m, ep


def test_build_writes_owned_parquet_sorted_and_keyed(tmp_path):
    m, _ = _mgr(tmp_path)
    df = m.build_or_refresh("sonarr")
    assert list(df.columns) == COLUMNS
    # Beta's episode is unowned → only Alpha's two owned eps, sorted by (s,e)
    assert df["tvdb_join_key"].tolist() == ["100:1:1", "100:1:2"]
    written = tmp_path / "sonarr" / "sonarr" / "owned_episodes.parquet"
    assert written.exists()
    assert df.equals(pd.read_parquet(written))


def test_crown_jewel_never_touches_episode_files_parquet(tmp_path):
    m, ep = _mgr(tmp_path)
    m.build_or_refresh("sonarr")
    # the only parquet written is owned_episodes.parquet — NOT episode_files.parquet
    files = {p.name for p in (tmp_path / "sonarr" / "sonarr").glob("*.parquet")}
    assert files == {"owned_episodes.parquet"}
    assert ep.cleanup_called is False               # never invoked the JIT pruner


def test_missing_caches_degrade_gracefully(tmp_path):
    m, _ = _mgr(tmp_path)
    m.sonarr_cache.episode_files = None
    df = m.build_or_refresh("sonarr")
    assert df.empty and list(df.columns) == COLUMNS


def test_constructs_via_real_init_and_builds(tmp_path):
    # exercises the REAL __init__ end-to-end — the path that broke in production with a
    # stray self.register() call (register() lives on a mixin this manager doesn't use;
    # BaseManager.__init__ already registers). logger/config/registry default internally.
    m = SonarrCacheOwnedEpisodesManager(
        logger=None, config=None, global_cache=_FakeCache(tmp_path),
        registry=None, sonarr_cache=_SonarrCache(_FakeEpisodeFiles()), dry_run=True)
    df = m.build_or_refresh("sonarr")
    assert df["tvdb_join_key"].tolist() == ["100:1:1", "100:1:2"]
