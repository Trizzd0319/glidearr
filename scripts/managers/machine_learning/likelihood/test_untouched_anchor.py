"""The UNTOUCHED-branch axis anchor (``untouched_base`` 12 -> 25, SCORER_REVISION 4).

The untouched branch is the only part of the likelihood model that consumes a RAW
``watchability_score`` at gain 1.0, so it is welded to the 0-100 scoring axis while the
cutoffs (75/45/20) and the engagement floors (50/64/78/90) are constants that never move
with it. Group D v2 translated that axis DOWN ~13 points and this branch absorbed the
whole translation: untouched titles reaching 1080p fell 456 -> 8 on the real library.

These tests pin the four invariants the re-anchor had to preserve, plus the two
properties that made a BASE (translation) the right instrument rather than a GAIN
(rescale). The derivation lives in watch_likelihood.py's "AXIS ANCHOR" block.
"""
import pytest

from scripts.managers.machine_learning.likelihood.watch_likelihood import (
    _DEFAULTS,
    explain_likelihood,
    profile_id_for_likelihood,
    resolution_cap_for_likelihood,
    watch_likelihood,
)

# The v1 -> v2 axis translation measured on the real library (owned-movie median 21 -> 8,
# file-owning-series median 21 -> 8). The re-anchor is the inverse of exactly this.
AXIS_SHIFT = 13
V1_BASE = 12.0


def _L(**row):
    return watch_likelihood(row, config=None)


def _v1(score):
    """The likelihood the OLD mapping (base 12) gave a v1 score — the target to preserve."""
    return min(_DEFAULTS["affinity_cap"], V1_BASE + score)


# ── invariant 1: taste reaches Remux-1080p, never 4K ─────────────────────────
def test_affinity_cap_stays_strictly_below_uhd_cutoff():
    assert _DEFAULTS["affinity_cap"] < _DEFAULTS["uhd_cutoff"]


def test_no_untouched_score_can_ever_reach_4k():
    """Not just at the observed maximum — at ANY score, including impossible ones."""
    for score in (0, 25, 50, 58, 71, 100, 1000):
        L = _L(watch_count=0, watchability_score=score)
        assert L <= _DEFAULTS["affinity_cap"]
        assert L < _DEFAULTS["uhd_cutoff"]
        assert resolution_cap_for_likelihood(L, config=None) in (720, 1080)
        assert profile_id_for_likelihood(L, config=None) in (3, 4, 7, 8)   # never 5/10/9


# ── invariant 2: the cold floor is still the floor ───────────────────────────
def test_cold_untouched_title_lands_at_the_720p_floor():
    cold = _L(watch_count=0, watchability_score=0)
    assert cold == _DEFAULTS["untouched_base"]
    assert cold < _DEFAULTS["fhd_cutoff"]
    assert resolution_cap_for_likelihood(cold, config=None) == 720
    assert profile_id_for_likelihood(cold, config=None) == 3


def test_crossing_hd_cutoff_has_no_behavioural_consequence():
    """base 25 sits above hd_cutoff (20) where 12 sat below it. That is safe ONLY because
    hd_res and floor_res are the same resolution — pin it, so a future change to either
    cannot silently promote every cold title off the floor."""
    assert _DEFAULTS["hd_res"] == _DEFAULTS["floor_res"] == 720
    below = resolution_cap_for_likelihood(_DEFAULTS["hd_cutoff"] - 1, config=None)
    above = resolution_cap_for_likelihood(_DEFAULTS["hd_cutoff"] + 1, config=None)
    assert below == above == 720


def test_low_end_stays_steep_and_sticky():
    """It must still take real affinity to climb off 720p — the escape distance is 20
    score points (45 - 25), against 15 for a gain-based correction of the same strength."""
    escape = _DEFAULTS["fhd_cutoff"] - _DEFAULTS["untouched_base"]
    assert escape >= 20
    assert resolution_cap_for_likelihood(
        _L(watch_count=0, watchability_score=escape - 1), config=None) == 720
    assert resolution_cap_for_likelihood(
        _L(watch_count=0, watchability_score=escape), config=None) == 1080


# ── invariant 3: a title that earned 1080p on the old axis still earns it ────
@pytest.mark.parametrize("v1_score", [33, 35, 40, 43, 50, 53, 58, 62, 71])
def test_titles_that_earned_a_tier_on_the_old_axis_still_earn_it(v1_score):
    """A title whose v1 score cleared a rung clears the SAME rung once its score is
    re-expressed on the v2 axis (v1 - the measured 13-point translation)."""
    v2_score = v1_score - AXIS_SHIFT
    old = _v1(v1_score)
    new = _L(watch_count=0, watchability_score=v2_score)
    assert resolution_cap_for_likelihood(new, config=None) == \
           resolution_cap_for_likelihood(old, config=None)
    assert profile_id_for_likelihood(new, config=None) == \
           profile_id_for_likelihood(old, config=None)


def test_the_1080p_bar_moved_by_exactly_the_axis_shift():
    """The score an untouched title needs for 1080p: 33 before, 20 after — the axis moved
    13 and so did the bar. This is the single number the whole re-anchor is about."""
    def bar(base):
        return next(s for s in range(0, 101)
                    if min(_DEFAULTS["affinity_cap"], base + s) >= _DEFAULTS["fhd_cutoff"])
    assert bar(V1_BASE) == 33
    assert bar(_DEFAULTS["untouched_base"]) == 20
    assert bar(V1_BASE) - bar(_DEFAULTS["untouched_base"]) == AXIS_SHIFT


# ── invariant 4: watched titles are untouched by this change ─────────────────
@pytest.mark.parametrize("watch_count,expected_floor", [(1, 50), (2, 64), (3, 78), (4, 90), (9, 90)])
def test_engagement_floors_are_unchanged_and_still_decide(watch_count, expected_floor):
    """Every watched title whose ENGAGEMENT floor won under the old mapping still gets
    exactly that floor: the floors are constants, and affinity is compared to them with
    the same max(). Checked at score 0 (no affinity) AND at the score that would have
    been just under the floor on the old axis."""
    for score in (0, expected_floor - V1_BASE - 1 - AXIS_SHIFT):
        row = {"watch_count": watch_count, "watchability_score": max(0, score)}
        assert watch_likelihood(row, config=None) == expected_floor
        assert profile_id_for_likelihood(expected_floor, config=None) == \
               profile_id_for_likelihood(expected_floor, config=None)


def test_4k_stays_reserved_for_rewatch_engagement():
    assert resolution_cap_for_likelihood(_L(watch_count=3), config=None) == 2160
    assert resolution_cap_for_likelihood(
        _L(watch_count=0, watchability_score=100), config=None) == 1080
    # ...and the 4K rungs are only reachable from the engagement side.
    assert profile_id_for_likelihood(_L(watch_count=4), config=None) == 9


def test_watched_tiering_is_byte_identical_where_the_floor_wins():
    """The exhaustive form of invariant 4: over the whole (watch_count, score) grid, any
    title whose floor beat its OLD affinity gets an IDENTICAL likelihood now."""
    for wc in range(1, 6):
        for v1_score in range(0, 75):
            old_aff = _v1(v1_score)
            floor = min(_DEFAULTS["rewatch_floor"],
                        _DEFAULTS["watched_floor"] + (wc - 1) * _DEFAULTS["rewatch_step"])
            if old_aff > floor:
                continue                      # affinity decided — re-anchored on purpose
            new = watch_likelihood({"watch_count": wc,
                                    "watchability_score": max(0, v1_score - AXIS_SHIFT)},
                                   config=None)
            assert new == floor


# ── why a base and not a gain ────────────────────────────────────────────────
def test_gain_is_untouched_so_the_mapping_stays_order_isomorphic():
    """Gain 1.0 + no saturation below the cap ⇒ the affinity ordering is exactly the
    score ordering. A gain > 1 pins high scorers at affinity_cap, and titles sharing one
    value carry no ranking information — the precise failure Group D v2 removed."""
    assert _DEFAULTS["untouched_score_gain"] == 1.0
    # Measured on the real library: the highest score any UNTOUCHED title carries is 41
    # on the v2 axis (it was 53 on v1 — the library-wide max of 58 belongs to a WATCHED
    # title, which the cap is allowed to bind). ZERO untouched titles reach the cap, so
    # the affinity ordering over the whole realised range is strictly increasing.
    UNTOUCHED_V2_MAX = 41
    seq = [_L(watch_count=0, watchability_score=s) for s in range(0, UNTOUCHED_V2_MAX + 1)]
    assert all(b > a for a, b in zip(seq, seq[1:]))          # no ties anywhere
    assert max(seq) < _DEFAULTS["affinity_cap"]
    # And the headroom before saturation is no smaller than the old mapping's: the first
    # saturating score is 49 (25+49=74) against an untouched max of 41, where base 12
    # saturated at 62 against an untouched max of 53. 8 points of headroom, against 9.
    first_sat = next(s for s in range(0, 200)
                     if _DEFAULTS["untouched_base"] + s >= _DEFAULTS["affinity_cap"])
    assert first_sat - UNTOUCHED_V2_MAX >= 8
    # A gain-based correction of equivalent strength does NOT have this property.
    gain_cfg = {"watch_likelihood": {"untouched_base": 12, "untouched_score_gain": 2.2}}
    gain_seq = [watch_likelihood({"watch_count": 0, "watchability_score": s}, config=gain_cfg)
                for s in range(0, UNTOUCHED_V2_MAX + 1)]
    assert len(set(gain_seq)) < len(gain_seq)               # ties at the cap — a constant again


def test_base_is_the_measured_axis_shift():
    assert _DEFAULTS["untouched_base"] == V1_BASE + AXIS_SHIFT


# ── percentile mode is scale-invariant and was NOT re-anchored ───────────────
def test_percentile_mode_is_unaffected_by_the_base():
    """``untouched_mode: "percentile"`` reads a RANK, so it never sees the score axis.
    Its output must be identical whatever untouched_base/gain are set to."""
    def L(pct, base, gain):
        cfg = {"watch_likelihood": {"untouched_mode": "percentile", "untouched_pct_floor": 0,
                                    "untouched_base": base, "untouched_score_gain": gain}}
        return watch_likelihood({"watch_count": 0, "watchability_score": 5,
                                 "watchability_percentile": pct}, config=cfg)
    for pct in (0, 25, 50, 90, 97, 99.9, 100):
        assert L(pct, 12.0, 1.0) == L(pct, 25.0, 1.0) == L(pct, 40.0, 2.5)


def test_percentile_floor_still_gates_the_top_slice_only():
    """``untouched_pct_floor`` is a threshold on the same rank scale — also unaffected."""
    cfg = {"watch_likelihood": {"untouched_mode": "percentile", "untouched_pct_floor": 90}}
    row = lambda p: {"watch_count": 0, "watchability_score": 58, "watchability_percentile": p}
    assert watch_likelihood(row(50), config=cfg) == 0.0            # below the floor → nothing
    assert watch_likelihood(row(100), config=cfg) == _DEFAULTS["affinity_cap"]
    assert watch_likelihood(row(95), config=cfg) < _DEFAULTS["uhd_cutoff"]


def test_percentile_mode_falls_back_to_absolute_when_the_column_is_absent():
    cfg = {"watch_likelihood": {"untouched_mode": "percentile"}}
    assert watch_likelihood({"watch_count": 0, "watchability_score": 10}, config=cfg) == \
           _DEFAULTS["untouched_base"] + 10


# ── the explanation must not drift from the number ──────────────────────────
def test_explain_matches_the_number_on_the_new_base():
    ex = explain_likelihood({"watch_count": 0, "watchability_score": 20}, config=None)
    assert ex["engagement_tier"] == "untouched"
    assert ex["winner"] == "affinity"
    assert ex["likelihood"] == ex["affinity"] == _DEFAULTS["untouched_base"] + 20
    assert ex["likelihood"] == _DEFAULTS["fhd_cutoff"]           # exactly the 1080p bar


def test_config_override_still_wins():
    cfg = {"watch_likelihood": {"untouched_base": 12}}
    assert watch_likelihood({"watch_count": 0, "watchability_score": 0}, config=cfg) == 12.0
