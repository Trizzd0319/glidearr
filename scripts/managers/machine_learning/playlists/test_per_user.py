"""Tests for playlists/per_user — personalize a household watchability score by a
user's genre affinity (tilt_score), the affinity-match primitive (genre_match), and
the live ranker (priority_score: affinity > JIT > household)."""
from __future__ import annotations

from scripts.managers.machine_learning.playlists.per_user import (
    genre_match,
    kids_household_affinity,
    priority_score,
    tilt_score,
)


def test_kids_household_affinity_weights_genres_by_score():
    # Genres accumulate weighted by household score; emitted in the genre_match-consumable shape.
    aff = kids_household_affinity([(["Animation", "Family"], 80), (["Animation", "Adventure"], 40),
                                  (["Comedy"], 0)])
    assert aff == {"animation": 120.0, "family": 80.0, "adventure": 40.0}   # zero-score show dropped
    # … and it tilts the kid's ranking toward the household's kid genres.
    assert genre_match(["Animation"], aff) > genre_match(["Comedy"], aff)


def test_kids_household_affinity_ignores_unusable_scores():
    assert kids_household_affinity([]) == {}
    assert kids_household_affinity([(["A"], None), (["B"], "x"), (["C"], -5)]) == {}
    assert kids_household_affinity([(["A"], float("nan"))]) == {}            # NaN = no signal


def test_no_tilt_or_missing_signal_returns_household():
    assert tilt_score(65, ["Animation"], {"animation": 50}, tilt_pct=0) == 65      # tilt off
    assert tilt_score(65, [], {"animation": 50}, tilt_pct=50) == 65                # no genres
    assert tilt_score(65, ["Animation"], {}, tilt_pct=50) == 65                    # no affinity
    assert tilt_score(0, ["Animation"], {"animation": 50}, tilt_pct=50) == 0       # no base score


def test_top_genre_keeps_full_score():
    assert tilt_score(40, ["Action"], {"action": 100}, tilt_pct=50) == 40.0        # match=1 → full


def test_unwatched_genre_discounted_to_floor():
    # user watches only Action; a Kids series → match 0 → keep floor (1-0.5) of the score
    assert tilt_score(65, ["Kids"], {"action": 100}, tilt_pct=50) == 32.5


def test_tilt_100_drops_zero_affinity_to_zero():
    assert tilt_score(65, ["Kids"], {"action": 100}, tilt_pct=100) == 0.0


def test_case_insensitive_partial_match_averages():
    # ACTION matches (1.0), Comedy absent (0.0) → avg 0.5 → 40*(0.5+0.5*0.5)=30
    assert tilt_score(40, ["ACTION", "Comedy"], {"Action": 100}, tilt_pct=50) == 30.0


def test_reranks_kids_below_adult_show_for_an_adult_user():
    aff = {"action": 100, "drama": 80}                          # adult taste, no kids genres
    bluey = tilt_score(65, ["Animation", "Family", "Kids"], aff, tilt_pct=50)   # high household
    action = tilt_score(45, ["Action", "Drama"], aff, tilt_pct=50)             # lower household
    assert action > bluey                                       # personalization flips the order


# ── genre_match ───────────────────────────────────────────────────────────────
def test_genre_match_none_when_no_signal():
    assert genre_match([], {"action": 1}) is None
    assert genre_match(["Action"], {}) is None
    assert genre_match(["Mystery"], {"action": 1}) == 0.0       # genre absent → 0, not None


def test_genre_match_normalizes_to_top_genre():
    # Action is the user's top (max) genre → 1.0; mean of [1.0] = 1.0
    assert genre_match(["Action"], {"action": 100, "drama": 50}) == 1.0
    # ACTION(1.0) + Comedy(absent 0.0) → mean 0.5
    assert genre_match(["ACTION", "Comedy"], {"Action": 100}) == 0.5


# ── genre_match modes (precision=legacy | soft | coverage | blend) ─────────────
_AFF4 = {"animation": 1, "comedy": 1, "family": 1, "adventure": 1}   # 'children' absent
_BLUEY = ["Animation", "Children", "Comedy", "Family"]              # 3 matched + 1 off-taste
_ARCHIE = ["Animation", "Comedy", "Family"]                         # same 3, no extra
_GEORGE = ["Adventure", "Animation", "Children", "Comedy", "Family"]  # also covers Adventure


def test_precision_mode_is_unchanged_legacy_default():
    assert genre_match(_BLUEY, _AFF4) == 0.75                       # (1+0+1+1)/4, default mode
    assert genre_match(_BLUEY, _AFF4, mode="precision") == 0.75     # explicit precision identical


def test_soft_mode_discounts_extra_offtaste_genre():
    # zero-affinity 'Children' counts only soft_lambda in the denominator: 3/(3+0.5*1)=0.857
    v = genre_match(_BLUEY, _AFF4, mode="soft", soft_lambda=0.5)
    assert round(v, 3) == 0.857
    assert genre_match(_ARCHIE, _AFF4, mode="soft") == 1.0         # no extras → unchanged
    assert genre_match(_BLUEY, _AFF4, mode="soft", soft_lambda=0.0) == 1.0  # lambda 0 → extras free


def test_coverage_mode_ties_same_matched_genres_and_rewards_breadth():
    bluey = genre_match(_BLUEY, _AFF4, mode="coverage")
    archie = genre_match(_ARCHIE, _AFF4, mode="coverage")
    george = genre_match(_GEORGE, _AFF4, mode="coverage")
    assert abs(bluey - archie) < 0.01     # same 3 covered genres → ~tie (was 0.75 vs 1.00)
    assert archie > bluey                 # purer show wins only by the hair-thin tiebreak
    assert george > bluey                 # covering Adventure too = more of the user's taste


def test_blend_mode_sits_between_precision_and_coverage():
    show = _BLUEY
    prec = genre_match(show, _AFF4, mode="precision")              # 0.75
    cov = genre_match(show, _AFF4, mode="coverage")                # ~0.75 (tiebreak)
    blend = genre_match(show, _AFF4, mode="blend", blend_weight=0.5)
    assert min(prec, cov) - 0.01 <= blend <= max(prec, cov) + 0.01


def test_unknown_mode_falls_back_to_precision():
    assert genre_match(_BLUEY, _AFF4, mode="bogus") == genre_match(_BLUEY, _AFF4)


# ── priority_score: precedence user-affinity > JIT > household ─────────────────
_AFF, _JIT, _HH = 0.9, 0.65, 0.1       # the live default weights


def _p(h, a, jit=False):
    return priority_score(h, a, is_jit=jit, affinity_weight=_AFF,
                          jit_weight=_JIT, household_weight=_HH)


def test_jit_beats_a_merely_household_popular_show():
    household_fav = _p(1.0, 0.0, jit=False)     # max household, no taste match, not JIT
    jit_item      = _p(0.0, 0.0, jit=True)      # zero household, no match, but JIT'd
    assert jit_item > household_fav             # 0.5 > 0.1


def test_strong_affinity_outranks_jit():
    strong_aff = _p(0.0, 1.0, jit=False)        # perfect taste match, not JIT
    jit_item   = _p(1.0, 0.0, jit=True)         # JIT'd + popular but off-taste
    assert strong_aff > jit_item                # 0.9 > 0.6  (measured against affinity)


def test_weak_affinity_loses_to_jit():
    weak_aff = _p(0.0, 0.4, jit=False)          # 0.9*0.4 = 0.36
    jit_item = _p(0.0, 0.0, jit=True)           # 0.5
    assert jit_item > weak_aff                  # JIT wins when affinity is weak


def test_on_taste_and_jit_tops_everything():
    best = _p(1.0, 1.0, jit=True)               # 0.9 + 0.65 + 0.1
    assert abs(best - 1.65) < 1e-9
    assert best > _p(1.0, 1.0, jit=False)       # the JIT boost still adds on top


def test_none_affinity_treated_as_zero():
    assert _p(0.5, None) == _HH * 0.5           # no taste signal → pure household
