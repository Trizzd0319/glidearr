"""
Unit tests for whole-file deletion protection in SonarrCacheEpisodeFilesManager.

These tests are self-contained (no network / no Sonarr) — they build synthetic
DataFrames and exercise:

  * ``_build_protected_file_ids`` — the whole-file protected set.
  * ``_do_delete_marked_files`` (dry_run) — that no row whose ``episode_file_id``
    is shared with a guarded sibling is ever marked for deletion.

The bug being guarded against: a single physical file in Sonarr can back
several episode rows (multi-episode files share one ``episodeFileId``).  The
per-row guards only inspect the row being processed, so a watched/grace-expired
episode could ``DELETE episodefile/{id}`` and silently destroy a sibling that
is pilot / keep / recent-air / household protected.

Run directly:  python -m scripts.managers.services.sonarr.cache.test_episode_files_guards
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from scripts.managers.services.sonarr.cache.episode_files import (
    SonarrCacheEpisodeFilesManager as M,
)


class _StubLogger:
    """Swallow log calls; record warnings so tests can inspect guard messages."""
    def __init__(self):
        self.warnings: list[str] = []
        self.infos: list[str] = []

    def log_warning(self, msg):  self.warnings.append(str(msg))
    def log_info(self, msg):     self.infos.append(str(msg))
    def log_debug(self, msg):    pass
    def log_error(self, msg):    pass


def _mgr() -> M:
    """Build a manager instance without running __init__ (no deps needed)."""
    mgr = M.__new__(M)
    mgr.logger = _StubLogger()
    mgr.dry_run = True
    # Arm the deletion hard gate (deletions_enabled): these tests exercise the
    # delete pass's per-row guards, so the pass itself must be allowed to run.
    mgr.config = {"free_space_limit": 100.0}
    return mgr


_NOW = datetime(2026, 6, 6, tzinfo=timezone.utc)


def _iso(days_ago: float) -> str:
    return (_NOW - timedelta(days=days_ago)).isoformat()


def _row(**kw) -> dict:
    """A schema-conformant row with sensible non-guarding defaults."""
    base = {
        "episode_file_id":        None,
        "series_id":              1,
        "series_title":           "Test Show",
        "season_number":          1,
        "episode_number":         1,
        "is_pilot":               False,
        "is_watched":             True,
        "next_episode":           False,
        "watch_count":            1,
        "percent_complete":       100,
        "last_watched_at":        _iso(10),
        "available_until":        _iso(9),
        "marked_for_deletion":    False,
        "keep_policy":            None,
        "air_date_utc":           _iso(400),   # long ago → never recent-air
        "all_household_watched":  True,
        "size_bytes":             1_000_000_000,
    }
    base.update(kw)
    return base


def _df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


# ──────────────────────────────────────────────────────────────────────────────
# Scenarios
# ──────────────────────────────────────────────────────────────────────────────

def scenario_recent_air_sibling():
    """E02 (watched, marked) shares file 22491 with E03 (recently aired, unmarked).
    The whole file must be protected — E02 must NOT be deleted."""
    rows = [
        # real pilot so de-facto-pilot logic doesn't auto-protect E02's file
        _row(series_id=1, season_number=1, episode_number=1,
             is_pilot=True, episode_file_id=11001),
        _row(series_id=1, season_number=1, episode_number=2,
             episode_file_id=22491, marked_for_deletion=True),
        _row(series_id=1, season_number=1, episode_number=3,
             episode_file_id=22491, marked_for_deletion=False,
             air_date_utc=_iso(5)),   # aired 5d ago → recent-air guard
    ]
    return _df(rows), {22491}


def scenario_household_sibling():
    """E02 (marked) shares file 22492 with E03 (not all household watched)."""
    rows = [
        _row(series_id=2, season_number=1, episode_number=1,
             is_pilot=True, episode_file_id=11002),
        _row(series_id=2, season_number=1, episode_number=2,
             episode_file_id=22492, marked_for_deletion=True),
        _row(series_id=2, season_number=1, episode_number=3,
             episode_file_id=22492, marked_for_deletion=False,
             all_household_watched=False),
    ]
    return _df(rows), {22492}


def scenario_keep_season_sibling():
    """Partial keep_policy population: E01 in latest season has keep_season,
    its file-sibling E02 doesn't but is marked. Whole file protected."""
    rows = [
        _row(series_id=5, season_number=1, episode_number=1,
             is_pilot=True, episode_file_id=11005),
        # latest non-special season is 2
        _row(series_id=5, season_number=2, episode_number=1,
             episode_file_id=50000, keep_policy="keep_season",
             marked_for_deletion=False),
        _row(series_id=5, season_number=2, episode_number=2,
             episode_file_id=50000, keep_policy=None,
             marked_for_deletion=True),
    ]
    return _df(rows), {50000}


def scenario_independent_delete():
    """A single-episode file with no guard on any sibling IS deleted."""
    rows = [
        _row(series_id=3, season_number=1, episode_number=1,
             is_pilot=True, episode_file_id=11003),
        _row(series_id=3, season_number=1, episode_number=5,
             episode_file_id=30000, marked_for_deletion=True),
    ]
    return _df(rows), set()  # nothing protected (30000 deletable)


def scenario_coalesce_unguarded():
    """Two marked rows share file 40000, neither guarded → one delete, one coalesced."""
    rows = [
        _row(series_id=4, season_number=1, episode_number=1,
             is_pilot=True, episode_file_id=11004),
        _row(series_id=4, season_number=1, episode_number=8,
             episode_file_id=40000, marked_for_deletion=True),
        _row(series_id=4, season_number=1, episode_number=9,
             episode_file_id=40000, marked_for_deletion=True),
    ]
    return _df(rows), set()


# ──────────────────────────────────────────────────────────────────────────────
# Assertions
# ──────────────────────────────────────────────────────────────────────────────

def _check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(f"{name}: {detail}")


def test_protected_set():
    print("test_build_protected_file_ids:")
    mgr = _mgr()
    for fn in (scenario_recent_air_sibling, scenario_household_sibling,
               scenario_keep_season_sibling):
        df, expected = fn()
        got = set(mgr._build_protected_file_ids(df, _NOW))
        # the shared multi-ep file id must be present
        _check(f"{fn.__name__}: expected fid(s) {expected} subset of protected {got}",
               expected.issubset(got), f"missing {expected - got}")

    # independent file must NOT be protected
    df, _ = scenario_independent_delete()
    got = set(mgr._build_protected_file_ids(df, _NOW))
    _check("independent file 30000 not protected", 30000 not in got, f"got {got}")


def test_delete_pass():
    print("test_do_delete_marked_files (dry_run):")

    # 1. recent-air sibling → E02 not deleted, shared-file guard fires
    mgr = _mgr()
    df, _ = scenario_recent_air_sibling()
    _, stats = mgr._do_delete_marked_files("inst", df)
    _check("recent-air: deleted == 0", stats["deleted"] == 0, f"stats={stats}")
    _check("recent-air: skipped_shared_file == 1",
           stats["skipped_shared_file"] == 1, f"stats={stats}")
    _check("recent-air: E02 flag cleared",
           bool(df.loc[df["episode_number"] == 2, "marked_for_deletion"].iloc[0]) is False)

    # 2. household sibling → not deleted
    mgr = _mgr()
    df, _ = scenario_household_sibling()
    _, stats = mgr._do_delete_marked_files("inst", df)
    _check("household: deleted == 0", stats["deleted"] == 0, f"stats={stats}")
    _check("household: skipped_shared_file == 1",
           stats["skipped_shared_file"] == 1, f"stats={stats}")

    # 3. keep_season sibling (partial policy) → not deleted
    mgr = _mgr()
    df, _ = scenario_keep_season_sibling()
    _, stats = mgr._do_delete_marked_files("inst", df)
    _check("keep_season: deleted == 0", stats["deleted"] == 0, f"stats={stats}")
    _check("keep_season: skipped_shared_file == 1",
           stats["skipped_shared_file"] == 1, f"stats={stats}")

    # 4. independent file → deleted once, no shared-file guard
    mgr = _mgr()
    df, _ = scenario_independent_delete()
    _, stats = mgr._do_delete_marked_files("inst", df)
    _check("independent: deleted == 1", stats["deleted"] == 1, f"stats={stats}")
    _check("independent: skipped_shared_file == 0",
           stats["skipped_shared_file"] == 0, f"stats={stats}")
    _check("independent: bytes_freed > 0", stats["bytes_freed"] > 0, f"stats={stats}")

    # 5. coalesce of two unguarded rows sharing one file → one delete, one coalesced
    mgr = _mgr()
    df, _ = scenario_coalesce_unguarded()
    _, stats = mgr._do_delete_marked_files("inst", df)
    _check("coalesce: deleted == 1", stats["deleted"] == 1, f"stats={stats}")
    _check("coalesce: coalesced_multiep == 1",
           stats["coalesced_multiep"] == 1, f"stats={stats}")
    _check("coalesce: skipped_shared_file == 0",
           stats["skipped_shared_file"] == 0, f"stats={stats}")


if __name__ == "__main__":
    test_protected_set()
    test_delete_pass()
    print("\nAll guard tests passed")
