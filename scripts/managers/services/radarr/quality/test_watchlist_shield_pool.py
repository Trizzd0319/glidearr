"""radarr/quality/test_watchlist_shield_pool.py — the shield on the COORDINATOR pool.

``RadarrSpacePressureManager.build_delete_candidates`` is a SECOND implementation of the
movie delete pool (the coordinator calls it; ``run_deletions`` uses the brain planner). A
guard added to only one of the two would hold under the single-service fallback and
silently do nothing under the coordinator — which is the path that actually runs. These
tests pin both halves of the shield's behaviour on that method.
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.services.radarr.quality.space_pressure import RadarrSpacePressureManager


class _Logger:
    def __init__(self):
        self.msgs: list = []

    def log_info(self, m):
        self.msgs.append(str(m))

    def log_warning(self, m):
        self.msgs.append(str(m))

    def log_debug(self, m):
        pass

    def log_error(self, m):
        pass


def _mgr():
    m = object.__new__(RadarrSpacePressureManager)
    m.logger = _Logger()
    m.config = {"space_pressure_score_ceiling": 17, "space_exhaustive_downgrade": False}
    m.global_cache = None
    m.registry = None
    m.radarr_api = None
    m.dry_run = True
    return m


def _df(hold=None):
    row = {
        "movie_id": 1, "movie_file_id": 500, "tmdb_id": 4242, "title": "Wanted Film",
        "keep_policy": None, "is_franchise_entry": False, "last_watched_at": None,
        "marked_for_deletion": False, "size_bytes": 8 * 1024 ** 3, "resolution": 720,
        "watchability_score": 3,                 # far under the 17 ceiling → tier-1 candidate
        "date_added": "2024-01-01T00:00:00Z",
    }
    if hold is not None:
        row["watchlist_hold"] = hold
    df = pd.DataFrame([row])
    df["is_franchise_entry"] = df["is_franchise_entry"].astype(bool)
    return df


def _fids(mgr, df, **kw):
    return {c["fid"] for c in mgr.build_delete_candidates("standard", df, **kw)}


def test_shield_removes_the_title_from_the_coordinator_pool():
    m = _mgr()
    assert _fids(m, _df(hold=True)) == set()
    assert m.last_skipped_watchlist == 1
    assert any("WATCHLIST shield" in s for s in m.logger.msgs)


def test_the_same_title_enters_the_pool_once_the_shield_releases():
    m = _mgr()
    assert _fids(m, _df(hold=False)) == {500}
    assert m.last_skipped_watchlist == 0


def test_absent_column_is_byte_identical():
    m = _mgr()
    assert _fids(m, _df()) == {500}
    assert m.last_skipped_watchlist == 0


def test_the_4k_reclaim_pool_is_exempt():
    """``ignore_score_ceiling`` builds the dual-version reclaim pool, where only a 2160p
    BONUS copy is dropped and the 1080p baseline survives — the household keeps the
    watchlisted title either way, so the shield must not block pure reclaim (the same
    reason the score ceiling is relaxed there)."""
    m = _mgr()
    assert _fids(m, _df(hold=True), ignore_score_ceiling=True) == {500}
