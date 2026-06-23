"""Tests for demand-aware acquisition — the per-user breadth aggregation and the watchability×demand^t
blend (the policy that prioritizes broad-appeal grabs as space tightens)."""
from __future__ import annotations

from scripts.managers.machine_learning.acquisition.demand import demand_priority, demand_score

_ACTION = {"action": 1.0}
_COMEDY = {"comedy": 1.0}


def test_demand_counts_users_whose_taste_matches():
    # Three action fans + a comedy fan → an action title's demand ≈ 3 (the comedy fan doesn't count).
    affs = [_ACTION, _ACTION, _ACTION, _COMEDY]
    assert round(demand_score(["Action"], affs), 3) == 3.0
    assert round(demand_score(["Comedy"], affs), 3) == 1.0      # only the one comedy fan


def test_weak_match_below_threshold_is_not_demand():
    weak = {"action": 1.0, "drama": 1.0}                        # an Action title here matches ~0.5
    assert demand_score(["Action", "Romance", "War"], [weak], threshold=0.9) == 0.0   # diluted below 0.9
    assert demand_score(["Action"], [weak], threshold=0.4) > 0.0


def test_cold_start_user_contributes_the_popularity_prior():
    affs = [_ACTION, {}]                                        # one fan + one no-history account
    d = demand_score(["Action"], affs, popularity=0.5)
    assert round(d, 3) == 1.5                                   # 1.0 (fan) + 0.5 (cold prior)
    assert demand_score(["Comedy"], affs, popularity=0.0) == 0.0   # no match, no popularity → 0


def test_priority_is_demand_neutral_when_roomy():
    # t=0 → priority == watchability regardless of demand (abundance grabs broadly).
    assert demand_priority(70, 3, 0.0) == 70
    assert demand_priority(70, 0, 0.0) == 70


def test_priority_weights_breadth_at_the_floor():
    # t=1 → watchability × demand. A broad-appeal title beats a higher-watchability niche one.
    broad = demand_priority(70, 3, 1.0)                         # 210
    niche = demand_priority(90, 1, 1.0)                         # 90
    assert broad == 210 and niche == 90 and broad > niche


def test_zero_demand_is_dropped_once_tightening():
    assert demand_priority(90, 0, 1.0) == 0.0                   # nobody wants it + tight → not grabbed
    assert demand_priority(90, 0, 0.5) == 0.0
    assert demand_priority(90, 0, 0.0) == 90                    # …but fine in pure abundance


def test_priority_clamps_t_and_handles_bad_input():
    assert demand_priority(50, 2, 5) == demand_priority(50, 2, 1.0)   # t clamped to 1
    assert demand_priority(50, 2, -1) == 50                            # t clamped to 0
    assert demand_priority(None, 2, 1.0) == 0.0
