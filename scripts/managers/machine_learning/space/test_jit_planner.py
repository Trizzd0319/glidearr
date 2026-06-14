"""Tests for space.jit_planner — the pure decision slices of the Sonarr JIT next-episode
quality upgrade (ML Step 7c). The service keeps the stateful apply loop (projected_free
accounting, ep-id fetch, df writes, worker spawn); these cover the extracted cores.
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.machine_learning.space.jit_planner import (
    choose_jit_profile,
    jit_candidates,
    jit_reserve_gb,
    jit_row_skip,
    jit_step_down_pids,
    next_up_grab_candidates,
    target_tier_key,
)

_KIDS = {"g", "pg", "tv-g", "tv-y", "tv-y7"}


def _p(pid, res):
    return {"id": pid, "name": f"P{res}", "items": [{"allowed": True, "quality": {"resolution": res, "name": f"q{res}"}}]}


_BEST_FIRST = [_p(13, 2160), _p(12, 1080), _p(11, 720)]   # highest resolution first
_MEASURED = {"q2160": 200.0, "q1080": 70.0, "q720": 30.0}  # MiB/min — est(100min)=19.53/6.84/2.93 GiB


# ── candidate selection ───────────────────────────────────────────────────────────
def test_jit_candidates_filters_sorts_and_caps():
    df = pd.DataFrame([
        {"series_id": 1, "season_number": 1, "episode_number": 3, "next_episode": True,  "is_watched": False, "upgraded_for_watching": False},
        {"series_id": 1, "season_number": 1, "episode_number": 1, "next_episode": True,  "is_watched": False, "upgraded_for_watching": False},
        {"series_id": 1, "season_number": 1, "episode_number": 2, "next_episode": True,  "is_watched": False, "upgraded_for_watching": False},
        {"series_id": 1, "season_number": 1, "episode_number": 4, "next_episode": False, "is_watched": False, "upgraded_for_watching": False},  # not next
        {"series_id": 2, "season_number": 1, "episode_number": 1, "next_episode": True,  "is_watched": True,  "upgraded_for_watching": False},  # watched
        {"series_id": 2, "season_number": 1, "episode_number": 2, "next_episode": True,  "is_watched": False, "upgraded_for_watching": True},   # already upgraded
        {"series_id": 2, "season_number": 1, "episode_number": 3, "next_episode": True,  "is_watched": False, "upgraded_for_watching": False},
    ])
    out = jit_candidates(df, max_per_series=2)
    got = [(r["series_id"], r["episode_number"]) for _, r in out.iterrows()]
    # series 1: capped to the 2 lowest episodes (1,2), sorted; series 2: only ep 3 eligible.
    assert got == [(1, 1), (1, 2), (2, 3)]


def test_next_up_grab_candidates_keeps_all_missing_caps_on_disk():
    # Missing (no-file) next-up rows are kept in FULL (fresh acquisition); on-disk rows are
    # capped at upgrade_cap per series (re-quality). Both returned, season/episode-sorted.
    rows = []
    for en in range(1, 6):   # 5 ON-DISK next-up → should cap to 3
        rows.append({"series_id": 10, "season_number": 1, "episode_number": en, "next_episode": True,
                     "is_watched": False, "upgraded_for_watching": False, "episode_file_id": 1000 + en})
    for en in range(6, 9):   # 3 MISSING next-up → all kept
        rows.append({"series_id": 10, "season_number": 1, "episode_number": en, "next_episode": True,
                     "is_watched": False, "upgraded_for_watching": False, "episode_file_id": None})
    out = next_up_grab_candidates(pd.DataFrame(rows), upgrade_cap=3)
    on_disk = out[out["episode_file_id"].notna()]
    missing = out[out["episode_file_id"].isna()]
    assert sorted(on_disk["episode_number"].tolist()) == [1, 2, 3]   # on-disk capped to first 3
    assert sorted(missing["episode_number"].tolist()) == [6, 7, 8]   # ALL missing kept


# ── reserve ───────────────────────────────────────────────────────────────────────
def test_jit_reserve_gb():
    assert jit_reserve_gb(1000.0, 80.0, 0.05) == 80.0     # pct (50) < floor (80) -> floor
    assert jit_reserve_gb(2000.0, 80.0, 0.05) == 100.0    # pct (100) > floor (80) -> pct
    assert jit_reserve_gb(0.0, 80.0, 0.05) == 80.0        # total unknown -> floor only
    assert jit_reserve_gb(float("inf"), 80.0, 0.05) == float("inf")  # inf*pct (guarded by max)


# ── per-row guard ───────────────────────────────────────────────────────────────────
def test_jit_row_skip():
    assert jit_row_skip("keep_series", "tv-ma", 5, 1, _KIDS) == "keep"
    assert jit_row_skip("keep_season", "tv-ma", 5, 1, _KIDS) == "keep"
    assert jit_row_skip(None, "g", 5, 1, _KIDS) == "kids"
    assert jit_row_skip(None, "tv-ma", float("nan"), 1, _KIDS) == "no_file"
    assert jit_row_skip(None, "tv-ma", 5, float("nan"), _KIDS) == "no_sid"
    assert jit_row_skip(None, "tv-ma", 5, 1, _KIDS) is None


# ── target profile choice ─────────────────────────────────────────────────────────
def test_choose_jit_profile_best_that_fits():
    # plenty of room, cap allows 4K -> highest (id 13).
    assert choose_jit_profile(_BEST_FIRST, cap=2160, projected_free=30.0, reserve_gb=5.0,
                              runtime_min=100, measured=_MEASURED)["id"] == 13


def test_choose_jit_profile_respects_likelihood_cap():
    # cap 1080 excludes the 4K profile -> id 12.
    assert choose_jit_profile(_BEST_FIRST, cap=1080, projected_free=30.0, reserve_gb=5.0,
                              runtime_min=100, measured=_MEASURED)["id"] == 12


def test_choose_jit_profile_steps_down_to_fit_reserve():
    # free 10: 4K (−19.5) and 1080 (−6.84) breach the 5 GB reserve; 720 (−2.93) fits -> id 11.
    assert choose_jit_profile(_BEST_FIRST, cap=2160, projected_free=10.0, reserve_gb=5.0,
                              runtime_min=100, measured=_MEASURED)["id"] == 11


def test_choose_jit_profile_none_when_nothing_fits():
    # free 5, reserve 5: even 720 (−2.93) breaches -> None.
    assert choose_jit_profile(_BEST_FIRST, cap=2160, projected_free=5.0, reserve_gb=5.0,
                              runtime_min=100, measured=_MEASURED) is None


def test_choose_jit_profile_pressure_cap_default_is_byte_identical():
    # pressure_cap=None (default) -> the bare likelihood cap; same pick as without it.
    assert choose_jit_profile(_BEST_FIRST, cap=2160, projected_free=30.0, reserve_gb=5.0,
                              runtime_min=100, measured=_MEASURED, pressure_cap=None)["id"] == 13


def test_choose_jit_profile_pressure_cap_lowers_the_ceiling():
    # plenty of room (cap allows 4K) but a 1080 space-band ceiling -> id 12, not 13.
    assert choose_jit_profile(_BEST_FIRST, cap=2160, projected_free=30.0, reserve_gb=5.0,
                              runtime_min=100, measured=_MEASURED, pressure_cap=1080)["id"] == 12
    # the ceiling only ever lowers: a 4K pressure_cap with a 1080 likelihood cap still -> 12.
    assert choose_jit_profile(_BEST_FIRST, cap=1080, projected_free=30.0, reserve_gb=5.0,
                              runtime_min=100, measured=_MEASURED, pressure_cap=2160)["id"] == 12


# ── step-down ladder ───────────────────────────────────────────────────────────────
def test_jit_step_down_pids():
    chosen = _p(12, 1080)
    assert jit_step_down_pids(_BEST_FIRST, chosen) == [12, 11]   # 1080 + 720, not 4K
    assert jit_step_down_pids(_BEST_FIRST, _p(13, 2160)) == [13, 12, 11]


# ── per-episode tier grouping key (deliverable B) ────────────────────────────────────
def test_target_tier_key_is_profile_max_resolution():
    assert target_tier_key(_p(13, 2160)) == 2160
    assert target_tier_key(_p(12, 1080)) == 1080
    assert target_tier_key(_p(11, 720)) == 720


def test_target_tier_key_groups_same_resolution_different_ids_together():
    # Two distinct profile ids at the SAME max resolution share a tier key → one search group,
    # and (because the key IS the top resolution) an identical step-down ladder. This is the
    # invariant that makes the grouped per-episode search free of over-grab.
    assert target_tier_key(_p(99, 1080)) == target_tier_key(_p(12, 1080)) == 1080
    assert jit_step_down_pids(_BEST_FIRST, _p(99, 1080)) == jit_step_down_pids(_BEST_FIRST, _p(12, 1080))
