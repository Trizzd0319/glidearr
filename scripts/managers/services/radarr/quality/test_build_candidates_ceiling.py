"""Tests for RadarrSpacePressureManager.build_delete_candidates' dual-version additions:
the default-off ``ignore_score_ceiling`` (used to build the 4K-copy reclaim pool, where every
baseline-backed 4K copy is pure reclaim regardless of watchability) and the new ``resolution``
field on each candidate. A stub manager (object.__new__) bypasses the heavy __init__.

The score-ceiling / universe-credit cases below pin ``space_exhaustive_downgrade=False``: their
2160p fixtures are ABOVE the 720p floor, and under the (default-on) exhaustive policy a still-
downgradable title can never be a delete candidate at all. The exhaustive floor invariant has its
own section at the bottom."""
from __future__ import annotations

import pandas as pd

from scripts.managers.services.radarr.quality.space_pressure import RadarrSpacePressureManager


class _StubLogger:
    def log_info(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_success(self, *a, **k): pass
    def log_error(self, *a, **k): pass
    def log_debug(self, *a, **k): pass


def _mgr(exhaustive: bool = False):
    m = object.__new__(RadarrSpacePressureManager)
    m.config = {"space_pressure_include_unwatched": True, "space_pressure_score_ceiling": 20,
                "space_exhaustive_downgrade": exhaustive}
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


# ── EXHAUSTIVE floor invariant: deletion is the true last resort ─────────────────
# Under space_exhaustive_downgrade (DEFAULT ON) a whole title may only enter the delete
# pool once it is AT or BELOW the 720p floor — i.e. nothing left to shrink.
def _floor_df():
    return pd.DataFrame([
        {"tmdb_id": 1, "movie_file_id": 11, "watchability_score": 1, "resolution": 2160,
         "size_bytes": 5 * 1024 ** 3, "title": "Still4K", "marked_for_deletion": True},
        {"tmdb_id": 2, "movie_file_id": 12, "watchability_score": 1, "resolution": 1080,
         "size_bytes": 5 * 1024 ** 3, "title": "Still1080", "marked_for_deletion": True},
        {"tmdb_id": 3, "movie_file_id": 13, "watchability_score": 1, "resolution": 720,
         "size_bytes": 5 * 1024 ** 3, "title": "AtFloor", "marked_for_deletion": True},
        {"tmdb_id": 4, "movie_file_id": 14, "watchability_score": 1, "resolution": 480,
         "size_bytes": 5 * 1024 ** 3, "title": "BelowFloor", "marked_for_deletion": True},
    ])


def test_exhaustive_delete_pool_only_admits_at_or_below_the_720_floor():
    m = _mgr(exhaustive=True)
    cands = m.build_delete_candidates("inst", _floor_df())
    # 1080p is NEVER deletable while it can still be stepped down; 720/480 are.
    assert {c["tmdb_id"] for c in cands} == {3, 4}
    assert m.last_skipped_downgradable == 2          # counted reason, not a silent drop


def test_exhaustive_off_reproduces_todays_pool_ignoring_resolution():
    cands = _mgr(exhaustive=False).build_delete_candidates("inst", _floor_df())
    assert {c["tmdb_id"] for c in cands} == {1, 2, 3, 4}


def test_exhaustive_empty_pool_is_explained_as_still_downgradable():
    m = _mgr(exhaustive=True)
    df = _floor_df().iloc[:2]                         # only the 2160p + 1080p rows
    assert m.build_delete_candidates("inst", df) == []
    assert m.last_skipped_downgradable == 2           # the coordinator reads this to say WHY


def test_exhaustive_unknown_resolution_stays_deletable():
    # The downgrade planner treats an unreadable resolution as nothing-to-step, so the delete
    # gate must mirror it — otherwise such a row could never be reclaimed by anything.
    df = pd.DataFrame([{"tmdb_id": 9, "movie_file_id": 99, "watchability_score": 1,
                        "resolution": None, "size_bytes": 5 * 1024 ** 3, "title": "Unknown",
                        "marked_for_deletion": True}])
    assert [c["tmdb_id"] for c in _mgr(exhaustive=True).build_delete_candidates("inst", df)] == [9]


def test_exhaustive_skipped_downgradable_excludes_keep_tagged_rows():
    # The counter must mean "would otherwise be deletable, but still has quality to shed" —
    # a keep_forever 4K title is never deletable AND never downgraded, so counting it would
    # overstate how much the downgrade pass has left to do.
    df = pd.DataFrame([
        {"tmdb_id": 1, "movie_file_id": 11, "watchability_score": 1, "resolution": 2160,
         "size_bytes": 5 * 1024 ** 3, "title": "Pinned", "marked_for_deletion": True,
         "keep_policy": "keep_forever"},
        {"tmdb_id": 2, "movie_file_id": 12, "watchability_score": 1, "resolution": 2160,
         "size_bytes": 5 * 1024 ** 3, "title": "Shrinkable", "marked_for_deletion": True,
         "keep_policy": None},
    ])
    m = _mgr(exhaustive=True)
    assert m.build_delete_candidates("inst", df) == []
    assert m.last_skipped_downgradable == 1


def test_exhaustive_floor_gate_exempts_the_4k_bonus_copy_pool():
    # A baseline-backed 2160p BONUS copy is pure reclaim (the 1080p survives → no title lost),
    # so the evict-4K-first pool must stay reachable exactly as the score ceiling is relaxed there.
    cands = _mgr(exhaustive=True).build_delete_candidates(
        "inst", _floor_df(), ignore_score_ceiling=True)
    assert {c["tmdb_id"] for c in cands} == {1, 2, 3, 4}
