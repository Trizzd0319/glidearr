"""C4 person-affinity threaded through the feature adapters + the shared owned-scorer gate.

test_c4_integration already proves the score_movie/score_show C4 math; these prove the
NEW plumbing: (1) score_movie_features / score_show_features forward person_weights +
person_affinity_cap (bump at cap=8, byte-identical at cap=0), and (2)
resolve_person_affinity_inputs forces cap=0.0 whenever the term is disabled or the
people-matrix affinity is empty — the guarantee that keeps un-built libraries unchanged."""
from __future__ import annotations

from scripts.managers.machine_learning.contracts.feature_rows import MovieFeatureRow, ShowFeatureRow
from scripts.managers.machine_learning.features.movie_features import score_movie_features
from scripts.managers.machine_learning.features.show_features import score_show_features
from scripts.managers.machine_learning.scoring._shared import resolve_person_affinity_inputs

CREDITS = {"cast": [{"name": "A", "id": 123, "order": 0}], "crew": []}


# ── adapter forwarding ────────────────────────────────────────────────────────────────
def test_movie_features_forwards_c4():
    fr = MovieFeatureRow(tmdb_id=603, genres=(), credits=CREDITS)
    ctx = dict(genre_affinity={}, watched_tmdb_ids=set(), collection_members={})
    base = score_movie_features(fr, **ctx)
    on = score_movie_features(fr, **ctx, person_weights={123: 5}, person_affinity_cap=8.0)
    off = score_movie_features(fr, **ctx, person_weights={123: 5}, person_affinity_cap=0.0)
    assert on > base                                         # household-favourite cast lifts the upgrade score
    assert off == base                                       # cap 0 → byte-identical


def test_show_features_forwards_c4():
    fr = ShowFeatureRow(tvdb_id=77, genres=(), credits=CREDITS,
                        watched_episodes=2, total_episodes=10)
    ctx = dict(genre_affinity={})
    base = score_show_features(fr, **ctx)
    on = score_show_features(fr, **ctx, person_weights={123: 5}, person_affinity_cap=8.0)
    off = score_show_features(fr, **ctx, person_weights={123: 5}, person_affinity_cap=0.0)
    assert on > base and off == base


# ── shared gate (the byte-identical safety valve) ───────────────────────────────────────
def test_gate_coerces_str_keys_and_caps_when_enabled():
    w, cap = resolve_person_affinity_inputs(
        {"scoring": {"person_affinity": {"enabled": True, "cap": 8.0}}},
        {"123": 5.0, "999": 4})
    assert w == {123: 5.0, 999: 4.0} and cap == 8.0


def test_gate_forces_cap_zero_when_disabled():
    _, cap = resolve_person_affinity_inputs(
        {"scoring": {"person_affinity": {"enabled": False, "cap": 8.0}}}, {"123": 5.0})
    assert cap == 0.0


def test_gate_forces_cap_zero_when_affinity_empty():
    # no people-matrix built → empty weights → byte-identical regardless of config
    _, cap = resolve_person_affinity_inputs({"scoring": {"person_affinity": {"enabled": True}}}, {})
    assert cap == 0.0


def test_gate_default_enabled_cap_when_config_absent():
    # an existing config with no scoring.person_affinity key still gets the live default ON
    w, cap = resolve_person_affinity_inputs({}, {"123": 5.0})
    assert w == {123: 5.0} and cap == 8.0
