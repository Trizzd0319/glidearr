"""Golden-vector tests for the Series & Saga Resumption ramp + priority math
(DESIGN_series_saga_resumption.md §4). The three worked examples in the doc are pinned exactly,
plus the ramp's branch boundaries, continuity across them, and the trigger-1 "return now" case."""
from __future__ import annotations

import math

from scripts.managers.machine_learning.acquisition.resumption_planner import (
    priority,
    ramp,
    resumption_priority,
)


# ── the ramp R(d): four branches, default W=60 R0=7 G=14 τ=30 ──────────────────────────
def test_ramp_branch_boundaries():
    assert ramp(100) == 0.0                       # d > W → ignore (too far out)
    assert ramp(60) == 0.0                        # d = W → R = 0 (ramp just starts)
    assert ramp(7) == 100.0                        # d = R0 → R = 100 (peaks a week early)
    assert ramp(0) == 100.0                        # release day → still max
    assert ramp(-14) == 100.0                      # d = −G → last of the grace window, still max


def test_ramp_linear_rise_matches_doc():
    # "approaching" branch: 100·(W−d)/(W−R0). Silo S2 in 40 days → ≈ 37.74 (doc rounds 37.7).
    assert abs(ramp(40) - 100.0 * 20 / 53) < 1e-9
    assert abs(ramp(40) - 37.7358) < 1e-3
    # Spider-Man: Brand New Day in 10 days → ≈ 94.34 (doc rounds 94.3).
    assert abs(ramp(10) - 100.0 * 50 / 53) < 1e-9
    assert abs(ramp(10) - 94.3396) < 1e-3


def test_ramp_post_grace_decay():
    # d < −G decays with constant τ: one τ past the grace edge → exp(−1) ≈ 0.3679.
    assert abs(ramp(-(14 + 30)) - 100.0 * math.exp(-1)) < 1e-9
    # half-life ≈ τ·ln2 ≈ 20.8 days past grace → ~50.
    assert abs(ramp(-(14 + 30 * math.log(2))) - 50.0) < 1e-6
    # long after release → tends to 0 but stays non-negative.
    assert 0.0 < ramp(-200) < 0.5


def test_ramp_is_continuous_across_boundaries():
    eps = 1e-6
    # at d = R0 (7): rise branch → 100 just above, hold = 100 at/below.
    assert abs(ramp(7 + eps) - 100.0) < 1e-3
    # at d = −G (−14): hold = 100, decay → ~100 just below.
    assert abs(ramp(-14 - eps) - 100.0) < 1e-3


def test_ramp_undated_or_garbage_is_zero():
    assert ramp(None) == 0.0                       # no date → no ramp (never floats undated up)
    assert ramp("soon") == 0.0


def test_ramp_honours_custom_window_params():
    # tighter window W=30 R0=3: 15 days out → 100·(30−15)/(30−3) = 55.55…
    assert abs(ramp(15, window_days=30, ready_by_days=3) - 100.0 * 15 / 27) < 1e-9
    # degenerate W==R0 (zero span) → max in-window rather than div-by-zero.
    assert ramp(5, window_days=7, ready_by_days=7) == 100.0


# ── the priority blend P = clamp(w_s·S_prior + w_r·R, 0, 100) ──────────────────────────
def test_priority_worked_examples_from_doc():
    # Silo 40 days, S_prior 82: P = 0.5·82 + 0.5·37.74 ≈ 59.87 (doc rounds 59.8).
    assert abs(priority(82, ramp(40)) - 59.868) < 1e-2
    # Silo 5 days out: ramp = 100 → P = 0.5·82 + 0.5·100 = 91 exactly.
    assert priority(82, ramp(5)) == 91.0
    # Spider-Man 10 days, S_prior 70: P = 0.5·70 + 0.5·94.34 ≈ 82.17 (doc rounds 82.2).
    assert abs(priority(70, ramp(10)) - 82.17) < 1e-2


def test_priority_clamps_and_coerces():
    assert priority(100, 100, weight_affinity=0.8, weight_proximity=0.8) == 100.0   # over-weighted → clamp
    assert priority(None, 50) == 25.0              # non-numeric S_prior → 0 → 0.5·0 + 0.5·50
    assert priority(50, None) == 25.0              # non-numeric ramp → 0


# ── end-to-end resumption_priority (ramp + blend + trigger-1 "return now") ─────────────
def test_resumption_priority_full_chain():
    assert abs(resumption_priority(82, 40) - 59.868) < 1e-2          # Silo, 40 days out
    assert resumption_priority(70, 5) == priority(70, 100.0)         # inside ready window → max ramp


def test_resumption_priority_trigger1_return_now_is_max_ramp():
    # days_to_release None == "I'm returning NOW" → d := ready_by_days → ramp 100.
    assert resumption_priority(82, None) == 91.0
    assert resumption_priority(82, None) == priority(82, 100.0)


def test_resumption_priority_reads_config_overrides():
    cfg = {"ramp_window_days": 30, "ready_by_days": 3, "weight_affinity": 0.7, "weight_proximity": 0.3}
    r = ramp(15, window_days=30, ready_by_days=3)
    assert abs(resumption_priority(60, 15, config=cfg) - (0.7 * 60 + 0.3 * r)) < 1e-9
