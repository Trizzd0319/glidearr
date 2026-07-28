"""The score→profile ladder, re-anchored to the household's REAL score distribution.

The rungs used to be 80/70/60/50/35/0 — absolute guesses. Measured max watchability was
58 (radarr) / 69 (sonarr), so the 70 and 80 rungs were unreachable: no title could ever
qualify for 4K and every owned 4K title was slated to demote. These tests pin the two
properties that failure violated — every rung is REACHABLE, and the ladder is anchored to
percentiles rather than to guesses — plus the shape/override contract.
"""
from __future__ import annotations

from scripts.managers.machine_learning.scoring._shared import (
    QUALITY_PROFILE_THRESHOLDS,
    ladder_rung_for_resolution,
    resolve_quality_ladder,
    score_to_profile,
    select_profile_id,
)
from scripts.managers.machine_learning.sizing.size_model import target_resolution_for_score

# The measured title-level distribution of the FILE-OWNING population — 6,449 titles
# (2,076 movies + the 4,373 series owning >=1 episode file), scored with Group D v2.
# The previous anchor pooled in 7,600 file-less pilot STUBS, which sit at the bottom of
# the score axis and dragged every percentile down ~6-8 points; a rung calibrated on
# titles that own no file cannot say anything about what quality to hold a file at.
# Percentile → score.
MEASURED = {50: 8, 75: 15, 90: 19, 95: 23, 97: 25, 98: 29, 99: 33, 99.5: 38, 99.9: 50}
MEASURED_MAX = 58          # highest title score in the real store under Group D v2

#: Titles at/above each rung on that same population, and how many of the household's
#: 67 owned-4K titles (62 movies + 5 series) each rung would keep at 2160p.
MEASURED_ADMITS = {50: 7, 38: 34, 33: 67, 29: 130, 25: 224}
OWNED_4K_TITLES = 67


# ── reachability: the bug ────────────────────────────────────────────────────

def test_every_rung_is_reachable_on_the_real_distribution():
    for threshold, label in QUALITY_PROFILE_THRESHOLDS:
        assert threshold <= MEASURED_MAX, (
            f"rung {threshold} ({label}) exceeds the highest score any title reaches "
            f"({MEASURED_MAX}) — it can never fire")


def test_at_least_one_title_can_reach_the_top_rung():
    top = max(t for t, _ in QUALITY_PROFILE_THRESHOLDS)
    assert score_to_profile(MEASURED_MAX) == QUALITY_PROFILE_THRESHOLDS[0][1]
    assert top <= MEASURED_MAX


def test_a_4k_rung_exists_and_is_reachable():
    uhd = ladder_rung_for_resolution("2160", default=None)
    assert uhd is not None
    assert uhd <= MEASURED_MAX
    assert "2160" in score_to_profile(uhd)
    assert "2160" not in score_to_profile(uhd - 1)


def test_the_older_rungs_would_have_failed_this_gate():
    """Documents WHY the numbers moved, twice over.

    The ORIGINAL guesses (80/70/60/50/35) put the top two rungs above anything the
    distribution produces. The FIRST re-anchor (62/46/41/36/35) fixed reachability but
    was calibrated on a stub-inclusive pool AND on Group D v1's near-constant +12, so
    every rung sat above the v2 distribution's p99.9."""
    guessed = [80, 70, 60, 50, 35, 0]
    assert [r for r in guessed if r > MEASURED_MAX] == [80, 70, 60]
    first_reanchor = [62, 46, 41, 36, 35]
    assert all(r > MEASURED[99] for r in first_reanchor), (
        "every rung of the stub-anchored ladder sits above p99 of the file-owning "
        "population — that ladder cannot tier this distribution")


# ── the percentile anchoring ─────────────────────────────────────────────────

def test_rungs_sit_on_the_documented_percentiles():
    thresholds = [t for t, _ in QUALITY_PROFILE_THRESHOLDS]
    assert thresholds == [MEASURED[99.9], MEASURED[99.5], MEASURED[99],
                          MEASURED[98], MEASURED[97], 0]


def test_the_4k_rung_admits_far_fewer_titles_than_the_household_curates():
    """PINS A KNOWN, DELIBERATE SHORTFALL so it cannot be rediscovered as a surprise.

    The 4K entry rung is the literal p99.5 of the file-owning population (38), which
    admits 34 titles — against the 67 the household actually keeps at 2160p today. Two
    effects compound: the file-owning pool is 2.2x smaller than the stub-inclusive one
    it replaced (a fixed percentile therefore admits proportionally fewer TITLES), and
    Group D v2 compressed the top of the distribution. The rung that reproduces the
    household's own 4K judgement is p99 (33). An operator who wants that sets
    ``scoring.quality_ladder``; the shipped ladder stays on the literal percentiles."""
    uhd = ladder_rung_for_resolution("2160", default=None)
    assert uhd == MEASURED[99.5] == 38
    assert MEASURED_ADMITS[uhd] < OWNED_4K_TITLES
    # ...and the rung that WOULD match the household's curation is documented, so the
    # override is a one-line change rather than a re-derivation.
    assert MEASURED_ADMITS[MEASURED[99]] == OWNED_4K_TITLES


def test_ladder_is_strictly_descending_with_a_zero_floor():
    thresholds = [t for t, _ in QUALITY_PROFILE_THRESHOLDS]
    assert thresholds == sorted(thresholds, reverse=True)
    assert len(set(thresholds)) == len(thresholds)     # no duplicate rungs
    assert thresholds[-1] == 0                          # nothing falls through to "SD"


def test_shape_preserved_six_rungs_same_labels():
    labels = [lbl for _, lbl in QUALITY_PROFILE_THRESHOLDS]
    assert labels == ["Remux 2160p", "Remux 2160p", "Remux 1080p",
                      "Bluray 1080p", "WEBDL 1080p", "HD 720p"]


def test_median_title_lands_on_the_floor():
    """Half the library is background weight — it must not be promoted off 720p."""
    assert score_to_profile(MEASURED[50]) == "HD 720p"
    assert score_to_profile(0) == "HD 720p"


# ── config overridability ────────────────────────────────────────────────────

def test_default_ladder_when_unconfigured():
    assert resolve_quality_ladder(None) == list(QUALITY_PROFILE_THRESHOLDS)
    assert resolve_quality_ladder({}) == list(QUALITY_PROFILE_THRESHOLDS)
    assert resolve_quality_ladder({"scoring": {}}) == list(QUALITY_PROFILE_THRESHOLDS)


def test_config_ladder_is_honoured():
    cfg = {"scoring": {"quality_ladder": [[90, "Remux 2160p"], [10, "HD 720p"]]}}
    ladder = resolve_quality_ladder(cfg)
    assert ladder == [(90, "Remux 2160p"), (10, "HD 720p")]
    assert score_to_profile(95, ladder) == "Remux 2160p"
    assert score_to_profile(50, ladder) == "HD 720p"
    assert score_to_profile(5, ladder) == "SD"


def test_config_ladder_is_normalised_to_descending_order():
    cfg = {"scoring": {"quality_ladder": [[0, "HD 720p"], [50, "Remux 1080p"]]}}
    ladder = resolve_quality_ladder(cfg)
    assert ladder[0][0] == 50            # an out-of-order list would mis-tier every title
    assert score_to_profile(60, ladder) == "Remux 1080p"


def test_malformed_config_falls_back_instead_of_raising():
    for bad in ("nonsense", [["x", "y"]], [[1]], [None], 42):
        assert resolve_quality_ladder({"scoring": {"quality_ladder": bad}}) == \
               list(QUALITY_PROFILE_THRESHOLDS)


def test_select_profile_id_uses_the_override():
    profiles = [
        {"id": 1, "name": "HD 720p", "items": [{"allowed": True, "quality": {"resolution": 720}}]},
        {"id": 2, "name": "Remux 2160p", "items": [{"allowed": True, "quality": {"resolution": 2160}}]},
    ]
    strict = [(90, "Remux 2160p"), (0, "HD 720p")]
    assert select_profile_id(50, profiles) == 2          # calibrated ladder → 4K
    assert select_profile_id(50, profiles, None, strict) == 1   # override → 720p


# ── the parallel ladder in size_model must track, not drift ──────────────────

def test_size_model_reads_the_same_rungs():
    uhd = ladder_rung_for_resolution("2160", default=None)
    fhd = ladder_rung_for_resolution("1080", default=None)
    assert target_resolution_for_score(uhd) == 2160
    assert target_resolution_for_score(uhd - 1) == 1080
    assert target_resolution_for_score(fhd) == 1080
    assert target_resolution_for_score(fhd - 1) == 720
    assert target_resolution_for_score(0) == 480
    assert target_resolution_for_score(None) == 480


def test_size_model_honours_a_config_override():
    cfg = {"scoring": {"quality_ladder": [[90, "Remux 2160p"], [80, "WEBDL 1080p"],
                                          [0, "HD 720p"]]}}
    assert target_resolution_for_score(95, config=cfg) == 2160
    assert target_resolution_for_score(85, config=cfg) == 1080
    assert target_resolution_for_score(50, config=cfg) == 720


def test_ladder_rung_for_resolution_picks_the_lowest_matching_rung():
    ladder = [(62, "Remux 2160p"), (46, "Remux 2160p"), (32, "WEBDL 1080p"), (0, "HD 720p")]
    assert ladder_rung_for_resolution("2160", ladder) == 46
    assert ladder_rung_for_resolution("1080", ladder) == 32
    assert ladder_rung_for_resolution("4320", ladder, default=-1) == -1
