"""Tests for RadarrSpacePressureManager.build_delete_candidates' dual-version additions:
the default-off ``ignore_score_ceiling`` (used to build the 4K-copy reclaim pool, where every
baseline-backed 4K copy is pure reclaim regardless of watchability) and the new ``resolution``
field on each candidate. A stub manager (object.__new__) bypasses the heavy __init__."""
from __future__ import annotations

import pandas as pd

from scripts.managers.services.radarr.quality.space_pressure import RadarrSpacePressureManager


class _StubLogger:
    def log_info(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_success(self, *a, **k): pass
    def log_error(self, *a, **k): pass
    def log_debug(self, *a, **k): pass


def _mgr():
    m = object.__new__(RadarrSpacePressureManager)
    m.config = {"space_pressure_include_unwatched": True, "space_pressure_score_ceiling": 20}
    m.logger = _StubLogger()
    m._get_movie_files_manager = lambda: None
    m._row_critic_avg = lambda df, idx: None
    m._universe_delete_age_days = lambda: None
    return m


def _df():
    return pd.DataFrame([
        {"tmdb_id": 1, "movie_file_id": 11, "watchability_score": 90, "resolution": 2160,
         "size_bytes": 5 * 1024 ** 3, "title": "HighWatch4K"},
        {"tmdb_id": 2, "movie_file_id": 12, "watchability_score": 5, "resolution": 2160,
         "size_bytes": 5 * 1024 ** 3, "title": "LowWatch4K"},
    ])


def test_score_ceiling_excludes_high_watchability_by_default():
    cands = _mgr().build_delete_candidates("inst", _df())
    tmdbs = {c["tmdb_id"] for c in cands}
    assert tmdbs == {2}                                    # score 90 is above the ceiling → excluded


def test_ignore_score_ceiling_includes_high_watchability():
    cands = _mgr().build_delete_candidates("inst", _df(), ignore_score_ceiling=True)
    tmdbs = {c["tmdb_id"] for c in cands}
    assert tmdbs == {1, 2}                                 # ceiling skipped → both 4K copies eligible


def test_candidates_carry_resolution():
    cands = _mgr().build_delete_candidates("inst", _df(), ignore_score_ceiling=True)
    assert all(c["resolution"] == 2160 for c in cands)


# ── borrowed franchise/universe credit on the COORDINATOR pool (mirrors run_deletions) ──
def _credit_df():
    # Two marked, low-score, old-watched movies; one carries hot saga credit, the other decayed.
    return pd.DataFrame([
        {"tmdb_id": 1, "movie_file_id": 11, "watchability_score": 5, "resolution": 2160,
         "size_bytes": 5 * 1024 ** 3, "title": "HotSaga", "marked_for_deletion": True,
         "universe_credit": 2.0},
        {"tmdb_id": 2, "movie_file_id": 12, "watchability_score": 5, "resolution": 2160,
         "size_bytes": 5 * 1024 ** 3, "title": "StaleSaga", "marked_for_deletion": True,
         "universe_credit": 0.4},
    ])


def test_universe_credit_protects_movie_from_coordinator_pool():
    # The coordinator twin must spare a hot-saga member (credit >= UNIVERSE_PROTECT_MIN) just like the
    # single-service run_deletions does — otherwise the committed deletion protection is bypassed
    # whenever the coordinator owns deletion. The decayed-credit sibling stays deletable.
    cands = _mgr().build_delete_candidates("inst", _credit_df())
    assert {c["tmdb_id"] for c in cands} == {2}


def test_universe_credit_guard_bypassed_for_uhd_reclaim_pool():
    # The 4K-copy reclaim pool (ignore_score_ceiling=True) is pure reclaim — the 1080p baseline
    # survives, so a hot-saga 4K BONUS copy loses no title and must remain reclaimable. The credit
    # guard is bypassed here, mirroring how the score ceiling is relaxed.
    cands = _mgr().build_delete_candidates("inst", _credit_df(), ignore_score_ceiling=True)
    assert {c["tmdb_id"] for c in cands} == {1, 2}
