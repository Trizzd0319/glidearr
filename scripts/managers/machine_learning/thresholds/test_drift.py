"""Tests for thresholds/drift.py — the axis-drift detector.

Pinned behaviours, in the order they matter:
  1. selectivity is STRICT and returns None (not 0.0) on an empty sample;
  2. the Group D v2 incident is CAUGHT — an axis that falls ~13 points fires;
  3. a legitimate re-anchor does NOT fire (that is the whole point);
  4. benign churn below the tolerance stays quiet;
  5. "could not tell" never reports as "ok".
"""
from scripts.managers.machine_learning.thresholds import drift


# ── selectivity ───────────────────────────────────────────────────────────────

def test_selectivity_is_strict():
    # A title exactly AT the cutoff is neither deleted nor restored — every
    # delete-family consumer compares strictly, so this must too.
    assert drift.selectivity([16, 17, 18], 17) == 100.0 / 3


def test_selectivity_none_on_empty():
    # Not 0.0 — an empty population has no selectivity, and 0.0 would claim the
    # cutoff admits nothing.
    assert drift.selectivity([], 17) is None
    assert drift.selectivity(None, 17) is None


def test_non_finite_scores_are_dropped_not_zeroed():
    # A NaN is an absent measurement. Counting it as 0.0 would fake downward drift.
    assert drift.selectivity([float("nan"), 50, 50], 17) == 0.0


def test_fingerprint_summary_is_none_on_empty():
    fp = drift.fingerprint([], {"x": 17})
    assert fp["n"] == 0
    assert fp["median"] is None and fp["mean"] is None and fp["max"] is None
    assert fp["shares"]["x"]["share_below"] is None


# ── the incident this module exists for ───────────────────────────────────────

def _pre_group_d():
    """A stand-in for the pre-Group-D-v2 owned-movie axis: median ~21."""
    return [max(0, 21 + (i % 25) - 12) for i in range(400)]


def _post_group_d():
    """The same population after the axis fell ~13 points: median ~8."""
    return [max(0, s - 13) for s in _pre_group_d()]


def test_group_d_v2_translation_is_caught():
    anchor = drift.build_anchor(_pre_group_d(), {"movie_delete_ceiling": 20},
                                label="radarr.owned_movies", revision=3)
    verdict = drift.assess(drift.compare(anchor, _post_group_d()))

    assert verdict["drifted"] is True
    assert verdict["severity"] == drift.SEVERITY_ALARM
    row = verdict["findings"][0]
    # The axis FELL, so a fixed cutoff now admits MORE of the population.
    assert row["delta_pp"] > 0


def test_a_correct_re_anchor_does_not_fire():
    """20 on the old axis and 17 on the new one preserve selectivity — that is
    exactly how the real re-anchor was derived, and it must read as OK."""
    anchor = drift.build_anchor(_pre_group_d(), {"movie_delete_ceiling": 20},
                                label="radarr.owned_movies", revision=3)
    # Someone re-anchors: the cutoff moves with the axis.
    verdict = drift.assess(
        drift.compare(anchor, _post_group_d(), cutoffs={"movie_delete_ceiling": 7})
    )
    assert verdict["drifted"] is False


def test_changed_cutoff_is_reported_but_does_not_drive_severity():
    anchor = drift.build_anchor(_pre_group_d(), {"c": 20}, label="p")
    cmp_ = drift.compare(anchor, _pre_group_d(), cutoffs={"c": 40})
    assert cmp_["rows"]["c"]["cutoff_changed"] is True
    # A deliberate edit is not axis drift.
    assert drift.assess(cmp_)["severity"] == drift.SEVERITY_OK


# ── tolerances ────────────────────────────────────────────────────────────────

def test_benign_churn_stays_quiet():
    """~5 pp is the observed movement of translation-immune percentile mode over
    the same window absolute cutoffs moved -98.2%. It must not fire."""
    base = [float(i % 100) for i in range(400)]
    anchor = drift.build_anchor(base, {"c": 50}, label="p")
    shifted = [s - 4 for s in base]          # ≈4 pp of share movement
    assert drift.assess(drift.compare(anchor, shifted))["drifted"] is False


def test_warn_below_alarm():
    base = [float(i % 100) for i in range(400)]
    anchor = drift.build_anchor(base, {"c": 50}, label="p")
    v = drift.assess(drift.compare(anchor, [s - 12 for s in base]))
    assert v["severity"] == drift.SEVERITY_WARN
    assert v["drifted"] is True


# ── "could not tell" is not "ok" ──────────────────────────────────────────────

def test_small_sample_is_unknown_not_ok():
    anchor = drift.build_anchor([float(i % 100) for i in range(400)],
                                {"c": 50}, label="p")
    verdict = drift.assess(drift.compare(anchor, [1.0, 2.0, 3.0]))
    assert verdict["severity"] == drift.SEVERITY_UNKNOWN
    # An unknown must NOT be reported as a clean bill of health.
    assert verdict["drifted"] is False
    assert verdict["findings"][0]["severity"] == drift.SEVERITY_UNKNOWN


def test_no_rows_is_unknown():
    assert drift.assess({"rows": {}})["severity"] == drift.SEVERITY_UNKNOWN


def test_empty_current_population_is_unknown_not_alarm():
    """The 456 -> 8 collapse shape: if the population vanishes entirely we cannot
    compute a share, and must say so rather than alarming on a phantom delta."""
    anchor = drift.build_anchor([float(i % 100) for i in range(400)],
                                {"c": 50}, label="p")
    assert drift.assess(drift.compare(anchor, []))["severity"] == drift.SEVERITY_UNKNOWN


# ── scale-agnosticism ─────────────────────────────────────────────────────────

def test_works_on_the_likelihood_scale_too():
    """untouched_base has no ThresholdSpec — it is on the likelihood scale, which
    is precisely why a THRESHOLD_SPECS-only re-anchor missed it. The detector
    must not care."""
    anchor = drift.build_anchor([float(i % 60) for i in range(400)],
                                {"untouched_base": 12}, label="likelihood.untouched")
    v = drift.assess(drift.compare(anchor, [float(i % 60) + 20 for i in range(400)]))
    assert v["drifted"] is True
    assert v["findings"][0]["name"] == "untouched_base"


# ── formatting ────────────────────────────────────────────────────────────────

def test_format_lines_never_raises_on_partial_input():
    lines = drift.format_lines(drift.assess(drift.compare({}, [])))
    assert lines and isinstance(lines[0], str)
