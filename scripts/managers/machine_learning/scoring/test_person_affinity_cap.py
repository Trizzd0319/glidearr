"""Group-C4 cap resolution — 8.0 by default, config-overridable, 0.0 when inert.

The cap is 8.0, matching C1 (collection completeness): "this is by people you keep
coming back to" is as strong a keep signal as "you are most of the way through this
collection". Anything larger would let a single taste term outweigh the critic
consensus tiers below +14.
"""
from __future__ import annotations

from scripts.managers.machine_learning.scoring._shared import (
    QUALITY_PROFILE_THRESHOLDS,
    person_affinity_score,
    resolve_person_affinity_inputs,
)

AFF = {"1245": 10.0, "3223": 5.0, "100": 8.0}


def test_default_cap_is_eight():
    weights, cap = resolve_person_affinity_inputs({}, AFF)
    assert cap == 8.0
    assert weights == {1245: 10.0, 3223: 5.0, 100: 8.0}      # str keys coerced to int


def test_default_cap_matches_the_c1_collection_cap():
    # C1's full-credit rung is +8 (see score_movie's C1 branch); C4 deliberately mirrors it.
    _, cap = resolve_person_affinity_inputs({}, AFF)
    assert cap == 8.0


def test_cap_is_config_overridable():
    _, cap = resolve_person_affinity_inputs(
        {"scoring": {"person_affinity": {"enabled": True, "cap": 3.5}}}, AFF)
    assert cap == 3.5


def test_disabled_forces_cap_zero():
    _, cap = resolve_person_affinity_inputs(
        {"scoring": {"person_affinity": {"enabled": False, "cap": 8.0}}}, AFF)
    assert cap == 0.0


def test_empty_matrix_forces_cap_zero():
    """The gate that kept C4 dead: no built people-matrix ⇒ no weights ⇒ cap 0.0."""
    _, cap = resolve_person_affinity_inputs({}, {})
    assert cap == 0.0
    _, cap_none = resolve_person_affinity_inputs({}, None)
    assert cap_none == 0.0


def test_garbage_cap_falls_back_to_eight():
    _, cap = resolve_person_affinity_inputs(
        {"scoring": {"person_affinity": {"cap": "lots"}}}, AFF)
    assert cap == 8.0


def test_score_never_exceeds_the_resolved_cap():
    weights, cap = resolve_person_affinity_inputs({}, AFF)
    media = {"cast": [1245, 3223, 100], "directors": [1245]}
    assert person_affinity_score(media, weights, cap) <= 8.0


def test_cap_saturates_at_eight_for_a_perfect_overlap():
    weights, cap = resolve_person_affinity_inputs({}, {"1": 10.0, "2": 10.0, "3": 10.0})
    assert person_affinity_score({"cast": [1, 2, 3]}, weights, cap) == 8.0


def test_c4_cap_cannot_alone_clear_the_top_ladder_rung():
    """Sanity bound: the strongest single C4 contribution must stay below the ladder's
    top rung, so a taste term can never be the sole reason a title reaches 4K."""
    top_rung = max(t for t, _ in QUALITY_PROFILE_THRESHOLDS)
    assert 8.0 < top_rung
