"""Unit tests for space.universe_quality — the pure universe quality-tier decision core
extracted from radarr/quality/universe (ML migration Step 7c).

The service-side parity gate lives in test_universe_floor_gate.py (it drives the delegating
service methods); these test the brain functions directly across their branches.
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.machine_learning.likelihood.watch_likelihood import (
    english_ladder_ids,
    ladder_rank,
    profile_id_for_likelihood,
    radarr_ladder_english,
)
from scripts.managers.machine_learning.space.universe_quality import (
    downgrade_single_rank,
    downgrade_target,
    universe_action,
    upgrade_target,
)

_GIB = 1024 ** 3

# Real-shape config: normal ladder + its parallel English-twin ladder (ids 12-19).
_LADDERS = {
    "radarr_quality_ladder":         [[0, 3], [20, 4], [30, 6], [40, 7], [55, 8], [65, 5], [70, 9], [85, 10]],
    "radarr_quality_ladder_english": [[0, 12], [20, 13], [30, 14], [40, 15], [55, 16], [65, 17], [70, 18], [85, 19]],
}


# ── band action ─────────────────────────────────────────────────────────────────
def test_universe_action_band():
    assert universe_action(8000.0, 9000.0, 9900.0) == "downgrade"   # below floor
    assert universe_action(9500.0, 9000.0, 9900.0) is None          # hold band [T,U]
    assert universe_action(9950.0, 9000.0, 9900.0) == "upgrade"     # above band top
    # Boundaries are inclusive of "hold" (strict < / >): free == T or == U -> hold.
    assert universe_action(9000.0, 9000.0, 9900.0) is None
    assert universe_action(9900.0, 9000.0, 9900.0) is None


# ── upgrade target (recalibrated default ladder: [0,3][45,4][55,7][65,8][75,5][82,10][90,9]) ──
def test_upgrade_target_steps_up_to_earned_tier():
    ranked = [{"id": 3}, {"id": 4}]
    # likelihood 48 -> earned profile 4 (rank 1) > current 3 (rank 0) -> step to id 4 (1080p starts at 45 now).
    assert upgrade_target(ranked, 3, 48, {})["id"] == 4


def test_upgrade_target_none_when_at_or_above_tier():
    ranked = [{"id": 3}, {"id": 4}]
    # likelihood 10 -> earned profile 3 (rank 0) <= current 4 (rank 1) -> no upgrade.
    assert upgrade_target(ranked, 4, 10, {}) is None


def test_upgrade_target_steps_down_to_highest_present_when_earned_absent():
    ranked = [{"id": 3}, {"id": 4}]                 # earned id 10 not configured
    # likelihood 85 -> earned profile 10 (rank 7) absent -> highest present above cur = id 4.
    assert upgrade_target(ranked, 3, 85, {})["id"] == 4


# ── single-rank downgrade (legacy / fallback) ─────────────────────────────────────
def test_downgrade_single_rank():
    ranked = [{"id": 10}, {"id": 11}, {"id": 12}]
    assert downgrade_single_rank(ranked, 12)["id"] == 11           # one rank down
    assert downgrade_single_rank(ranked, 10) is None               # at the floor
    assert downgrade_single_rank(ranked, 99) is None               # unknown -> ci=0 -> None


# ── resolution-tier downgrade ─────────────────────────────────────────────────────
_TIER = [
    {"id": 4, "items": [{"allowed": True, "quality": {"resolution": 480,  "name": "WEBDL-480p"}}]},
    {"id": 1, "items": [{"allowed": True, "quality": {"resolution": 720,  "name": "WEBDL-720p"}}]},
    {"id": 3, "items": [{"allowed": True, "quality": {"resolution": 1080, "name": "Remux-1080p"}}]},  # ~235
    {"id": 2, "items": [{"allowed": True, "quality": {"resolution": 1080, "name": "WEBDL-1080p"}}]},  # ~56
]


def test_downgrade_target_best_quality_tier():
    big = {"resolution": 2160, "size_bytes": int(50 * _GIB), "runtime_minutes": 100.0}
    assert downgrade_target(big, _TIER, 99, {})["id"] == 3          # Remux-1080p, best reduction
    small = {"resolution": 2160, "size_bytes": int(15 * _GIB), "runtime_minutes": 100.0}
    assert downgrade_target(small, _TIER, 99, {})["id"] == 2        # Remux too big -> WEBDL-1080p


def test_downgrade_target_unknown_resolution_falls_back_to_single_rank():
    unknown = {"resolution": None, "size_bytes": None, "runtime_minutes": None}
    # _TIER ids order [4,1,3,2]; current 3 at index 2 -> one rank down -> index 1 -> id 1.
    assert downgrade_target(unknown, _TIER, 3, {})["id"] == 1


# ── universe-credit step-down floor (a hot saga resists; a stale one drops) ─────────
def _urow(resolution, *, credit=None, size_gib=50.0):
    r = {"resolution": resolution, "size_bytes": int(size_gib * _GIB), "runtime_minutes": 100.0}
    if credit is not None:
        r["universe_credit"] = credit
    return r


def test_downgrade_target_cold_member_is_byte_identical():
    # No credit (or below the protect floor) -> floor stays 1, behaves exactly as before: a 720 file
    # steps to the next-lower tier (480, id 4), with or without a likelihood passed.
    cold = _urow(720, credit=0.0)
    assert downgrade_target(cold, _TIER, 99, {})["id"] == 4                  # no likelihood
    assert downgrade_target(cold, _TIER, 99, {}, likelihood=90)["id"] == 4   # likelihood but cold -> same


def test_downgrade_target_hot_member_holds_at_earned_4k():
    # Hot rewatched member: likelihood 90 -> cap 2160 -> floor 2160 -> a 2160 file can't step down (hold).
    hot = _urow(2160, credit=2.0)
    assert downgrade_target(hot, _TIER, 99, {}, likelihood=90) is None


def test_downgrade_target_hot_member_floors_at_earned_1080():
    # Hot but lower likelihood (64 -> cap 1080): a 2160 file steps to the best 1080 (Remux id 3) and
    # NO further; once AT 1080 it holds. It never drops to 720/480 like a cold member would.
    assert downgrade_target(_urow(2160, credit=2.0), _TIER, 99, {}, likelihood=64)["id"] == 3
    assert downgrade_target(_urow(1080, credit=2.0), _TIER, 99, {}, likelihood=64) is None


# ── English twin ladder (keeps English-locked films on the parallel English tiers) ──
def test_english_ladder_helpers():
    assert english_ladder_ids(_LADDERS) == {12, 13, 14, 15, 16, 17, 18, 19}
    assert radarr_ladder_english(_LADDERS)[0] == [0.0, 12]
    # Unset -> empty, so the feature is fully opt-in (off == normal ladder everywhere).
    assert radarr_ladder_english({}) == []
    assert english_ladder_ids({}) == set()


def test_profile_id_and_rank_english_selector():
    # likelihood 60 -> >=55 band -> English twin id 16 (vs normal id 8).
    assert profile_id_for_likelihood(60, config=_LADDERS, english=True) == 16
    assert profile_id_for_likelihood(60, config=_LADDERS, english=False) == 8
    # An English-twin id ranks on the English ladder, but is absent (-1) from the normal one
    # — exactly the bug the wiring fixes.
    assert ladder_rank(14, config=_LADDERS, english=True) == 2
    assert ladder_rank(14, config=_LADDERS, english=False) == -1


def test_upgrade_target_english_locked_climbs_english_ladder():
    ranked = [{"id": 12}, {"id": 13}]
    # English-locked on id 12 (rank 0), L=25 -> earned English twin 13 (rank 1) -> step to 13.
    assert upgrade_target(ranked, 12, 25, _LADDERS)["id"] == 13


def test_upgrade_target_english_none_when_at_or_above_tier():
    ranked = [{"id": 12}, {"id": 13}]
    # English on id 13 (rank 1), L=10 -> earned English twin 12 (rank 0) <= cur -> no upgrade.
    assert upgrade_target(ranked, 13, 10, _LADDERS) is None


def test_upgrade_target_english_steps_down_to_present_when_earned_absent():
    ranked = [{"id": 12}, {"id": 13}]                 # earned id 19 not configured in Radarr
    # English on 12, L=85 -> earned twin 19 (rank 7) absent -> highest present above cur = 13.
    assert upgrade_target(ranked, 12, 85, _LADDERS)["id"] == 13


def test_upgrade_target_normal_film_unchanged_when_english_ladder_configured():
    # REGRESSION GUARD: a non-English film (current id 3 ∉ english ids) must use the NORMAL
    # ladder even when the English ladder is configured -> byte-identical to pre-wiring.
    ranked = [{"id": 3}, {"id": 4}]
    assert upgrade_target(ranked, 3, 25, _LADDERS)["id"] == 4
    assert upgrade_target(ranked, 4, 10, _LADDERS) is None
