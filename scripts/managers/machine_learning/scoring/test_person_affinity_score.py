"""Group-C4 person_affinity_score — id-keyed, cap-gated, byte-identical at cap<=0."""
from __future__ import annotations

from scripts.managers.machine_learning.scoring._shared import person_affinity_score

WEIGHTS = {1245: 10, 3223: 5, 100: 8}        # int keys (must not hit affinity_topk's .lower())


def test_top3_mean_scaled_to_cap():
    media = {"cast": [1245, 3223], "directors": [100]}
    # contributions: 1.0, 0.5, 0.8 → top3 mean 0.7667 × cap4 ≈ 3.067
    assert person_affinity_score(media, WEIGHTS, 4.0) == 3.067


def test_cap_zero_is_byte_identical_zero():
    media = {"cast": [1245, 3223], "directors": [100]}
    assert person_affinity_score(media, WEIGHTS, 0.0) == 0.0
    assert person_affinity_score(media, WEIGHTS, -1.0) == 0.0


def test_empty_inputs_zero():
    assert person_affinity_score({}, WEIGHTS, 4.0) == 0.0
    assert person_affinity_score({"cast": [1245]}, {}, 4.0) == 0.0
    assert person_affinity_score({"cast": [999]}, WEIGHTS, 4.0) == 0.0   # no overlap


def test_int_keys_do_not_crash():
    # affinity_topk would AttributeError on int.lower(); this helper must not.
    assert person_affinity_score({"cast": [1245]}, {1245: 3}, 4.0) > 0


def test_role_weight_scales_contribution():
    # a composer (role weight 0.4) contributes less than a lead (1.0) for equal weight
    lead = person_affinity_score({"cast": [1245]}, {1245: 10}, 4.0)
    comp = person_affinity_score({"composers": [1245]}, {1245: 10}, 4.0)
    assert lead > comp > 0


def test_never_exceeds_cap():
    media = {"cast": [1245, 3223, 100]}
    assert person_affinity_score(media, {1245: 9, 3223: 9, 100: 9}, 4.0) <= 4.0
