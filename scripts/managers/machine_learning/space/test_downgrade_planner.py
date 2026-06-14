"""Tests for space.downgrade_planner — STEP-DOWN by resolution tier, spread.

Both planners step the lowest-watchability titles DOWN the resolution ladder one tier at
a time (4K->1080->720), spread across the eligible pool, until ~need_gb is reclaimed —
never crushing one title straight to the floor. Titles floor at ``floor_resolution``
(720); already-at-floor and would-upgrade (first-tier reclaim<=0) titles skip.

Ladder: 480/720/1080/2160 (ids 10/11/12/13). With unknown quality names the size model
falls back to the resolution table (480->12, 720->30, 1080->70, 2160->200 MiB/min), so a
100-min item: est(GiB)=rate*100/1024 -> 2160~19.5, 1080~6.84, 720~2.93.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from scripts.managers.machine_learning.space.downgrade_planner import (
    _profile_max_res,
    plan_movie_downgrades,
    plan_series_downgrades,
    step_targets,
)

_CUTOFF = datetime(2026, 1, 1, tzinfo=timezone.utc)
_GIB = 1024 ** 3

# representative profile per resolution tier (id encodes the tier)
_RANKED = [
    {"id": 10, "name": "P480",  "items": [{"allowed": True, "quality": {"resolution": 480,  "name": "q480"}}]},
    {"id": 11, "name": "P720",  "items": [{"allowed": True, "quality": {"resolution": 720,  "name": "q720"}}]},
    {"id": 12, "name": "P1080", "items": [{"allowed": True, "quality": {"resolution": 1080, "name": "q1080"}}]},
    {"id": 13, "name": "P2160", "items": [{"allowed": True, "quality": {"resolution": 2160, "name": "q2160"}}]},
]


# ── step_targets precompute parity (B2: profile_res hoisted once per plan) ───────
def test_step_targets_precomputed_profile_res_is_byte_identical():
    # The planners now pass profile_res (max-resolution per profile, computed once) to
    # avoid re-walking every profile per title. The precomputed path must produce the
    # EXACT same (targets, cum) as recomputing inside step_targets.
    def _est(p):
        # size monotonic in resolution so each tier is a real reduction from a big 4K file
        return {480: 1.0, 720: 2.0, 1080: 6.0, 2160: 19.0}[_profile_max_res(p)]
    precomp = [_profile_max_res(p) for p in _RANKED]
    for cur_res in (2160, 1080, 720, 480, "junk"):
        a_t, a_c = step_targets(_RANKED, cur_res, 30.0, _est, 720)
        b_t, b_c = step_targets(_RANKED, cur_res, 30.0, _est, 720, profile_res=precomp)
        assert [p["id"] for p in a_t] == [p["id"] for p in b_t] and a_c == b_c


# ── movies ────────────────────────────────────────────────────────────────────
def _row(movie_id, resolution, size_gib, runtime=100.0, keep_policy=None):
    return dict(
        movie_id=movie_id, resolution=resolution, quality_profile_name=f"p{resolution}",
        size_bytes=int(size_gib * _GIB), runtime_minutes=runtime, keep_policy=keep_policy,
        is_watched=False, last_watched_at=None, collection_name=None, title=f"m{movie_id}",
    )


def _plan(df, *, need_gb, score_map=None, protect_threshold=6):
    score_map = score_map if score_map is not None else {i: 0 for i in df.index}
    return plan_movie_downgrades(
        df, score_map, _RANKED, need_gb=need_gb, recent_cutoff=_CUTOFF,
        active_colls=set(), protect_threshold=protect_threshold, floor_resolution=720,
    )


def test_spread_one_tier_each_not_one_to_floor():
    # Two 4K titles; a small need is covered by ONE tier each (4K->1080). Neither is
    # crushed to the 720 floor — the downgrade is spread.
    df = pd.DataFrame([_row(1, 2160, 20.0), _row(2, 2160, 20.0)])
    cands, stats = _plan(df, need_gb=20.0, score_map={0: 0, 1: 5})
    assert {c["movie_id"] for c in cands} == {1, 2}
    assert all(c["target_id"] == 12 for c in cands)   # 1080, one tier down — NOT 720
    assert stats["target_met"]


def test_lowest_score_steps_first():
    df = pd.DataFrame([_row(1, 2160, 20.0), _row(2, 2160, 20.0)])
    cands, _ = _plan(df, need_gb=13.0, score_map={0: 0, 1: 5})
    assert [c["movie_id"] for c in cands] == [1]
    assert cands[0]["target_id"] == 12


def test_deep_pressure_steps_to_floor_only():
    df = pd.DataFrame([_row(1, 2160, 20.0), _row(2, 2160, 20.0)])
    cands, stats = _plan(df, need_gb=999.0, score_map={0: 0, 1: 5})
    assert all(c["target_id"] == 11 for c in cands)   # both floored at 720, not SD(480)
    assert not stats["target_met"]


def test_already_at_floor_skipped():
    df = pd.DataFrame([_row(1, 720, 5.0)])
    cands, stats = _plan(df, need_gb=50.0)
    assert cands == [] and stats["already_at_720p"] == 1


def test_first_tier_would_upgrade_is_skipped():
    # 1080 file but tiny 0.5 GiB: est(720,100)~2.9 > 0.5, stepping down re-grabs bigger -> skip.
    df = pd.DataFrame([_row(1, 1080, 0.5)])
    cands, stats = _plan(df, need_gb=50.0)
    assert cands == [] and stats["already_at_720p"] == 1


_TIER = [
    {"id": 1, "name": "P720",  "items": [{"allowed": True, "quality": {"resolution": 720,  "name": "WEBDL-720p"}}]},
    {"id": 3, "name": "Remux", "items": [{"allowed": True, "quality": {"resolution": 1080, "name": "Remux-1080p"}}]},   # ~235 MiB/min
    {"id": 2, "name": "WEB",   "items": [{"allowed": True, "quality": {"resolution": 1080, "name": "WEBDL-1080p"}}]},   # ~56 MiB/min
]


def test_appropriate_profile_per_tier_is_best_quality_reduction():
    # Big 4K file (50 GiB, 100 min): est(Remux-1080p)~23 GiB < 50, so the 1080 tier lands
    # in the BEST-quality profile that still reduces (Remux-1080p, id 3) sized from runtime
    # — NOT the absolute-lowest encode (WEBDL, id 2).
    df = pd.DataFrame([_row(1, 2160, 50.0)])
    cands, _ = plan_movie_downgrades(
        df, {0: 0}, _TIER, need_gb=5.0, recent_cutoff=_CUTOFF,
        active_colls=set(), protect_threshold=6, floor_resolution=720,
    )
    assert cands[0]["target_id"] == 3


def test_oversized_lower_tier_profile_excluded():
    # Small 4K file (15 GiB, 100 min): est(Remux-1080p)~23 GiB would be BIGGER than the
    # current file, so it's excluded; lands in WEBDL-1080p (~5.5 GiB, id 2) instead.
    df = pd.DataFrame([_row(1, 2160, 15.0)])
    cands, _ = plan_movie_downgrades(
        df, {0: 0}, _TIER, need_gb=2.0, recent_cutoff=_CUTOFF,
        active_colls=set(), protect_threshold=6, floor_resolution=720,
    )
    assert cands[0]["target_id"] == 2


def test_universe_left_to_universe_manager():
    df = pd.DataFrame([
        _row(1, 2160, 20.0, keep_policy="universe"),
        _row(2, 2160, 20.0, keep_policy="keep_universe"),
        _row(3, 2160, 20.0, keep_policy=None),
    ])
    cands, stats = _plan(df, need_gb=5.0, score_map={0: 0, 1: 0, 2: 0})
    assert [c["movie_id"] for c in cands] == [3]
    assert stats["skipped_protected"] == 2


# ── series (TV) ─────────────────────────────────────────────────────────────────
def _ep(series_id, resolution, size_gib, score, runtime_sec=1800, keep_policy=None,
        last_watched=None, air_date=None):
    return dict(
        series_id=series_id, watchability_score=score, keep_policy=keep_policy,
        size_bytes=int(size_gib * _GIB), series_title=f"s{series_id}", resolution=resolution,
        last_watched_at=last_watched, runtime_seconds=runtime_sec, air_date_utc=air_date,
    )


def _plan_series(df, *, need_gb, ceiling=20):
    return plan_series_downgrades(
        df, _RANKED, need_gb=need_gb, ceiling=ceiling, watch_cutoff=_CUTOFF, air_cutoff=_CUTOFF,
        keep_tags=frozenset({"keep_series", "keep_season"}), default_runtime_min=45.0,
        floor_resolution=720,
    )


def test_series_spread_one_tier_each():
    # Two 4K series (2 eps x 10 GiB each = 20 GiB); small need -> each steps ONE tier (->1080).
    df = pd.DataFrame([
        _ep(1, 2160, 10.0, 0), _ep(1, 2160, 10.0, 0),
        _ep(2, 2160, 10.0, 5), _ep(2, 2160, 10.0, 5),
    ])
    cands, stats = _plan_series(df, need_gb=20.0)
    assert {c["sid"] for c in cands} == {1, 2}
    assert all(c["target_id"] == 12 for c in cands)   # 1080, not 720
    assert stats["target_met"]


def test_series_lowest_score_first():
    df = pd.DataFrame([
        _ep(1, 2160, 10.0, 0), _ep(1, 2160, 10.0, 0),
        _ep(2, 2160, 10.0, 5), _ep(2, 2160, 10.0, 5),
    ])
    cands, _ = _plan_series(df, need_gb=15.0)
    assert [c["sid"] for c in cands] == [1]
    assert cands[0]["target_id"] == 12


def test_series_deep_pressure_floors_at_720():
    df = pd.DataFrame([_ep(1, 2160, 10.0, 0), _ep(1, 2160, 10.0, 0)])
    cands, stats = _plan_series(df, need_gb=999.0)
    assert [c["target_id"] for c in cands] == [11]    # floored at 720
    assert not stats["target_met"]


def test_series_guards():
    df = pd.DataFrame([
        _ep(1, 2160, 10.0, 0, keep_policy="keep_series"),            # keep -> protected
        _ep(2, 2160, 10.0, 99),                                      # high score
        _ep(3, 720, 10.0, 0),                                        # already at floor
        _ep(4, 2160, 10.0, 0, last_watched="2026-06-01T00:00:00Z"),  # recently watched
    ])
    cands, stats = _plan_series(df, need_gb=50.0)
    assert cands == []
    assert stats["skipped_protected"] == 1
    assert stats["skipped_high_score"] == 1
    assert stats["skipped_already"] == 1
    assert stats["skipped_recent"] == 1


def _plan_series_tier(df, *, need_gb):
    # Same as _plan_series but with the multi-1080p _TIER ladder (Remux vs WEBDL).
    return plan_series_downgrades(
        df, _TIER, need_gb=need_gb, ceiling=20, watch_cutoff=_CUTOFF, air_cutoff=_CUTOFF,
        keep_tags=frozenset(), default_runtime_min=45.0, floor_resolution=720,
    )


def test_series_appropriate_profile_per_tier_is_best_quality_reduction():
    # Big 4K series (4 eps x 10 GiB = 40 GiB, 30-min eps): est(whole-series Remux-1080p)
    # ~27.5 GiB < 40, so the 1080 tier lands in the BEST-quality profile that still reduces
    # (Remux-1080p, id 3), sized from runtime x episode count — NOT the cheapest (WEBDL).
    df = pd.DataFrame([_ep(1, 2160, 10.0, 0) for _ in range(4)])
    cands, _ = _plan_series_tier(df, need_gb=5.0)
    assert cands[0]["target_id"] == 3


def test_series_oversized_lower_tier_profile_excluded():
    # Small 4K series (4 eps x 5 GiB = 20 GiB): whole-series Remux-1080p (~27.5 GiB) would
    # be BIGGER than the current series -> excluded; lands in WEBDL-1080p (id 2).
    df = pd.DataFrame([_ep(1, 2160, 5.0, 0) for _ in range(4)])
    cands, _ = _plan_series_tier(df, need_gb=2.0)
    assert cands[0]["target_id"] == 2
