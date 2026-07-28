"""Tests for thresholds/derive.py + thresholds/registry.py.

Planted-truth recovery (a synthetic household whose score→P relationship is
known in closed form, so the derived cutoffs have a right answer), the documented
monotone-inverse edge cases, the §10 data gate, and the property the whole
shadow rollout rests on: in mode "shadow"/"off" every routed consumer gets back
EXACTLY the literal it passed in. Fixed seeds throughout. Runnable both ways:

    python -m pytest scripts/managers/machine_learning/thresholds/test_derive.py
    python scripts/managers/machine_learning/thresholds/test_derive.py
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np
import pandas as pd
import pytest

from scripts.managers.machine_learning.foundation import empirical_bayes_pool
from scripts.managers.machine_learning.thresholds import registry
from scripts.managers.machine_learning.thresholds.derive import (
    DEFAULT_SHRINKAGE_K,
    GATE_MILESTONES,
    MIN_POS_FOR_DERIVATION,
    MIN_POS_FOR_FIT,
    CalibrationResult,
    blend_threshold,
    confidence_label,
    data_gate,
    derive_all,
    derive_threshold,
    fit_calibrator,
    mature_candidates,
    shrinkage_weight,
)

# ── planted truth ─────────────────────────────────────────────────────────────
# P(watch within H | score) = logistic((score - MID) / SCALE). Its exact inverse
# is MID + SCALE * logit(p), so every derived cutoff has a closed-form target.
MID, SCALE = 50.0, 10.0


def planted_p(score):
    return 1.0 / (1.0 + np.exp(-(np.asarray(score, dtype=float) - MID) / SCALE))


def planted_score(p: float) -> float:
    return MID + SCALE * float(np.log(p / (1.0 - p)))


def synthetic(n: int = 6000, seed: int = 42, *, service: str = "radarr",
              mature: bool = True, source: str = "prospective",
              dates=None, watched=None) -> pd.DataFrame:
    """A pre-labeled snapshot frame with the planted score→P relationship."""
    rng = np.random.default_rng(seed)
    s = rng.uniform(0.0, 100.0, size=n)
    y = rng.random(n) < planted_p(s) if watched is None else np.full(n, bool(watched))
    if dates is None:
        dates = ["2026-06-01"] * n
    return pd.DataFrame({
        "snapshot_ts": [f"{d}T00:00:00+00:00" for d in dates],
        "snapshot_date": list(dates),
        "service": [service] * n,
        "instance": ["standard"] * n,
        "entity_id": [str(i) for i in range(n)],
        "watchability_score": s,
        "watched_within_h": y,
        "label_mature": [bool(mature)] * n,
        "source": [source] * n,
    })


# ── the data gate (§10) ───────────────────────────────────────────────────────

def test_data_gate_milestones():
    assert GATE_MILESTONES == (100, 300, 1000)
    assert MIN_POS_FOR_DERIVATION == 300
    for n in (0, 1, 37, 99):
        ok, why = data_gate(n)
        assert ok is False
        assert f"n_pos={n}" in why and "need 300" in why
        assert "insufficient data" in why
    ok, why = data_gate(100)          # past the power floor, below derivation
    assert ok is False and "insufficient data" in why
    ok, why = data_gate(299)
    assert ok is False
    ok, why = data_gate(300)          # the derivation milestone
    assert ok is True and "300" in why
    ok, why = data_gate(1000)
    assert ok is True


def test_data_gate_tolerates_garbage():
    assert data_gate(None)[0] is False
    assert data_gate("nope")[0] is False


def test_fit_no_longer_refuses_below_the_milestone():
    """The gate became a dial: below 300 positives the fit SUCCEEDS and is
    stamped gate_ok=False, and the caller shrinks it. The old hard refusal
    survives only as the opt-in `require_gate=True` diagnostic/backtest switch."""
    df = synthetic(n=400, seed=7)                    # ~200 positives < 300
    assert int(df["watched_within_h"].sum()) < MIN_POS_FOR_DERIVATION
    cal = fit_calibrator(df, 14)
    assert cal is not None and cal.gate_ok is False
    assert "insufficient data" in cal.gate_reason
    assert "NOT refused" in cal.gate_reason          # the wording is honest now
    assert fit_calibrator(df, 14, require_gate=True) is None


def test_fit_returns_none_on_degenerate_input():
    assert fit_calibrator(None, 14) is None
    assert fit_calibrator(pd.DataFrame(), 14) is None
    assert fit_calibrator(synthetic(n=10).drop(columns=["watchability_score"]), 14) is None
    # no labels and no history to build them from
    assert fit_calibrator(synthetic(n=5000).drop(
        columns=["watched_within_h", "label_mature"]), 14) is None
    # nothing matured
    assert fit_calibrator(synthetic(n=5000, mature=False), 14) is None
    # every row a positive (nothing for isotonic to separate)
    assert fit_calibrator(synthetic(n=5000, watched=True), 14) is None


def test_the_hard_floor_is_no_evidence_not_little_evidence():
    """Shrinkage is for "some evidence": under MIN_POS_FOR_FIT positives there is
    no derived term at all, so nothing can be blended and the constant stands."""
    assert MIN_POS_FOR_FIT >= 2
    df = synthetic(n=800, seed=42)
    df["watched_within_h"] = False
    df.loc[df.index[:MIN_POS_FOR_FIT - 1], "watched_within_h"] = True
    assert fit_calibrator(df, 14) is None            # one short of the floor
    df.loc[df.index[:MIN_POS_FOR_FIT], "watched_within_h"] = True
    cal = fit_calibrator(df, 14)
    assert cal is not None and cal.n_pos == MIN_POS_FOR_FIT
    assert cal.gate_ok is False


# ── shrinkage: the constant is the prior, the labels are the data ─────────────

def test_shrinkage_weight_is_monotone_and_half_at_k():
    ws = [shrinkage_weight(n) for n in (0, 1, 10, 38, 100, 150, 300, 1000, 10 ** 6)]
    assert ws == sorted(ws)                                  # monotone in n_pos
    assert all(a < b for a, b in zip(ws, ws[1:]))            # strictly, past 0
    assert ws[0] == 0.0
    assert shrinkage_weight(DEFAULT_SHRINKAGE_K) == pytest.approx(0.5, abs=1e-12)
    assert shrinkage_weight(50, k=50) == pytest.approx(0.5, abs=1e-12)
    assert shrinkage_weight(10 ** 9) == pytest.approx(1.0, abs=1e-6)
    # the documented ladder
    assert shrinkage_weight(38) == pytest.approx(0.2021, abs=5e-4)
    assert shrinkage_weight(100) == pytest.approx(0.4000, abs=5e-4)
    assert shrinkage_weight(300) == pytest.approx(0.6667, abs=5e-4)
    assert shrinkage_weight(1000) == pytest.approx(0.8696, abs=5e-4)
    # garbage and degenerate k never produce a NaN
    assert shrinkage_weight(None) == 0.0 and shrinkage_weight("x") == 0.0
    assert shrinkage_weight(-5) == 0.0
    assert shrinkage_weight(0, k=0) == 0.0                   # 0/(0+0) is not NaN
    assert shrinkage_weight(7, k=0) == 1.0                   # no prior at all


def test_zero_positives_is_bit_identical_to_the_constant():
    """THE cold-install contract: with no evidence the effective cutoff is not
    'about' the constant, it IS the constant — same float, same int round-trip."""
    for constant in (20, 30, 35, 75, 6, 0.0, 100.0, 31.7):
        eff, w = blend_threshold(None, constant, 0)
        assert eff == float(constant) and w == 0.0
        assert repr(eff) == repr(float(constant))
        # ...and a derived value that arrives with zero evidence is ignored too
        eff, w = blend_threshold(11.93, constant, 0)
        assert eff == float(constant) and w == 0.0
        eff, w = blend_threshold(float("nan"), constant, 5000)
        assert eff == float(constant) and w == 0.0


def test_blend_delegates_to_the_foundation_helper():
    """No second blend implementation: the effective value is exactly what
    foundation.empirical_bayes_pool returns for (child=derived, parent=constant),
    which is exactly the closed form w·derived + (1−w)·constant."""
    for n_pos, derived, constant in ((38, 11.93, 20.0), (150, 50.0, 30.0),
                                     (2000, 4.0, 75.0), (7, 99.0, 35.0)):
        eff, w = blend_threshold(derived, constant, n_pos)
        assert w == pytest.approx(n_pos / (n_pos + DEFAULT_SHRINKAGE_K), abs=1e-15)
        assert eff == pytest.approx(w * derived + (1 - w) * constant, abs=1e-12)
        assert eff == pytest.approx(float(empirical_bayes_pool(
            derived, float(n_pos), constant, k=DEFAULT_SHRINKAGE_K)), abs=0.0)


def test_blend_moves_monotonically_toward_the_derived_value():
    derived, constant = 12.0, 35.0
    effs = [blend_threshold(derived, constant, n)[0]
            for n in (0, 10, 38, 100, 150, 300, 1000, 10 ** 5)]
    assert effs == sorted(effs, reverse=True)          # walks constant -> derived
    assert effs[0] == constant
    assert effs[4] == pytest.approx((derived + constant) / 2.0, abs=1e-12)
    assert effs[-1] == pytest.approx(derived, abs=0.05)
    # a bigger k holds the constant longer — that is the whole knob
    assert blend_threshold(derived, constant, 150, k=1000)[0] > \
        blend_threshold(derived, constant, 150, k=150)[0]


def test_a_planted_relationship_with_plenty_of_labels_recovers_the_derived_value():
    """w -> 1: with thousands of positives the effective cutoff is the derived
    one (which is itself the planted truth), not a compromise with the constant."""
    cal = fit_calibrator(synthetic(n=20000, seed=42), 14, service="radarr")
    assert cal is not None and cal.n_pos > 5000
    for p, constant in ((0.2, 90.0), (0.5, 10.0), (0.8, 35.0)):
        derived = derive_threshold(cal, p)
        assert abs(derived - planted_score(p)) < 3.0
        eff, w = blend_threshold(derived, constant, cal.n_pos)
        assert w > 0.97
        assert abs(eff - derived) < 0.03 * abs(derived - constant) + 1e-9
        assert abs(eff - planted_score(p)) < 3.5


def test_confidence_is_an_annotation_on_the_same_ladder():
    assert confidence_label(0) == "none"
    assert confidence_label(1) == "directional"
    assert confidence_label(99) == "directional"
    assert confidence_label(GATE_MILESTONES[0]) == "usable"
    assert confidence_label(299) == "usable"
    assert confidence_label(GATE_MILESTONES[1]) == "stable"
    assert confidence_label(999) == "stable"
    assert confidence_label(GATE_MILESTONES[2]) == "magnitude-grade"
    assert confidence_label(10 ** 6) == "magnitude-grade"
    # no derived value -> no confidence to report, whatever n_pos says
    assert confidence_label(5000, has_derived=False) == "none"
    assert confidence_label(None) == "none" and confidence_label("x") == "none"


# ── planted-truth recovery ────────────────────────────────────────────────────

def test_recovers_planted_thresholds():
    cal = fit_calibrator(synthetic(n=6000, seed=42), 14, service="radarr")
    assert cal is not None and cal.gate_ok is True
    assert cal.n == 6000 and cal.n_pos >= MIN_POS_FOR_DERIVATION
    assert cal.ece is not None and cal.ece < 0.05      # honest probabilities
    for p in (0.2, 0.35, 0.5, 0.65, 0.8):
        got, want = derive_threshold(cal, p), planted_score(p)
        assert abs(got - want) < 3.0, (p, got, want)


def test_calibrated_probability_tracks_the_planted_curve():
    """Pointwise, isotonic on continuous scores is a fine mosaic of small blocks,
    so a single reading can sit ~0.1 off where the curve is steepest; averaged
    over the range it is tight (~0.015). Both properties are asserted, over
    several seeds — the mean is the one that matters for a threshold, since
    inverting integrates over the neighbourhood."""
    grid = np.linspace(2.0, 98.0, 200)
    for seed in (1, 7, 42):
        cal = fit_calibrator(synthetic(n=6000, seed=seed), 14)
        err = np.abs(cal.predict(grid) - planted_p(grid))
        assert np.max(err) < 0.15, seed
        assert np.mean(err) < 0.03, seed


def test_derived_rule_is_equivalent_to_the_probability_rule():
    """The point of the lower inverse: `score >= s*` must select EXACTLY the
    scores whose calibrated probability meets the target."""
    cal = fit_calibrator(synthetic(n=6000, seed=3), 14)
    grid = np.linspace(0.0, 100.0, 1001)
    for p in (0.1, 0.3, 0.5, 0.7, 0.9):
        s_star = derive_threshold(cal, p)
        by_score = grid >= s_star - 1e-9
        by_prob = cal.predict(grid) >= p - 1e-12
        assert np.array_equal(by_score, by_prob), p


def test_monotone_in_the_target():
    cal = fit_calibrator(synthetic(n=6000, seed=11), 14)
    xs = [derive_threshold(cal, p) for p in (0.05, 0.2, 0.4, 0.6, 0.8, 0.95)]
    assert xs == sorted(xs)


# ── documented inverse edge cases ─────────────────────────────────────────────

_BOUNDED = CalibrationResult(knots_score=(10.0, 40.0, 90.0),
                             knots_p=(0.20, 0.50, 0.80))


def test_target_below_range_clamps_to_zero():
    """target_p <= the map's lowest fitted value: every score already qualifies."""
    assert derive_threshold(_BOUNDED, 0.20) == 0.0
    assert derive_threshold(_BOUNDED, 0.05) == 0.0
    # On a real fit the floor is usually exactly 0, so only p<=0 can clamp — and
    # the smallest score meeting an epsilon target is a real, interior answer.
    cal = fit_calibrator(synthetic(n=6000, seed=5), 14)
    assert cal.p_min == 0.0
    assert derive_threshold(cal, cal.p_min) == 0.0
    assert 0.0 < derive_threshold(cal, 1e-12) < 10.0


def test_target_above_range_clamps_to_one_hundred():
    """target_p above the map's top: nothing qualifies — fail loud, not open."""
    assert derive_threshold(_BOUNDED, 0.80001) == 100.0
    assert derive_threshold(_BOUNDED, 1.0) == 100.0
    assert derive_threshold(_BOUNDED, 0.80) == 90.0        # exactly at the top


def test_plateau_inverse_returns_the_left_edge():
    """A hand-built two-block map: P=0.1 on [0,50), P=0.9 on [50,100]. Asking for
    0.1 must return the SMALLEST qualifying score (0), not somewhere inside the
    plateau — the documented lower-inverse convention."""
    cal = CalibrationResult(knots_score=(0.0, 49.0, 50.0, 100.0),
                            knots_p=(0.1, 0.1, 0.9, 0.9))
    assert derive_threshold(cal, 0.1) == 0.0
    assert derive_threshold(cal, 0.9) == 50.0
    assert derive_threshold(cal, 0.5) == pytest.approx(49.5, abs=1e-6)  # ramp
    assert derive_threshold(cal, 0.95) == 100.0


def test_derive_threshold_rejects_a_missing_calibrator():
    with pytest.raises(ValueError):
        derive_threshold(None, 0.5)
    with pytest.raises(ValueError):
        derive_threshold(CalibrationResult(), 0.5)
    cal = fit_calibrator(synthetic(n=6000, seed=5), 14)
    with pytest.raises(ValueError):
        derive_threshold(cal, float("nan"))
    with pytest.raises(ValueError):
        derive_threshold(cal, "high")


def test_custom_clamp_bounds_are_respected():
    cal = fit_calibrator(synthetic(n=6000, seed=9), 14)
    assert derive_threshold(cal, 1e-12, lo=5.0, hi=90.0) == 5.0
    assert derive_threshold(cal, 1.0, lo=5.0, hi=90.0) == 90.0


# ── maturity, provenance, temporal safety ─────────────────────────────────────

def test_immature_rows_never_reach_the_fit():
    good = synthetic(n=6000, seed=42)
    # 6000 adversarial rows: every LOW score marked watched. If maturity were
    # ignored the map would be dragged flat and the 0.5 cutoff would collapse.
    poison = synthetic(n=6000, seed=99, mature=False, watched=True)
    poison["watchability_score"] = np.linspace(0.0, 20.0, 6000)
    both = pd.concat([good, poison], ignore_index=True)
    cal = fit_calibrator(both, 14)
    assert cal is not None and cal.n == 6000
    assert abs(derive_threshold(cal, 0.5) - planted_score(0.5)) < 3.0


def test_backfill_rows_are_opt_in_at_the_function_level():
    """The pure estimator keeps its explicit `include_backfill=False`; the
    THRESHOLD CONFIG that calls it defaults true (see registry). Positives are
    counted per source either way, so provenance is never lost."""
    prospective = synthetic(n=6000, seed=42)
    backfill = synthetic(n=6000, seed=99, source="backfill")
    both = pd.concat([prospective, backfill], ignore_index=True)
    excl = fit_calibrator(both, 14)
    assert excl.n == 6000                                          # excluded
    assert set(excl.n_pos_by_source) == {"prospective"}
    incl = fit_calibrator(both, 14, include_backfill=True)
    assert incl.n == 12000
    assert set(incl.n_pos_by_source) == {"prospective", "backfill"}
    assert sum(incl.n_pos_by_source.values()) == incl.n_pos
    assert incl.n_pos_by_source["prospective"] == excl.n_pos
    assert fit_calibrator(backfill, 14) is None                    # nothing left
    assert fit_calibrator(backfill, 14, include_backfill=True).n == 6000


def test_fit_before_truncates_the_window():
    early = synthetic(n=6000, seed=42, dates=["2026-05-01"] * 6000)
    late = synthetic(n=6000, seed=43, dates=["2026-07-01"] * 6000)
    both = pd.concat([early, late], ignore_index=True)
    cal = fit_calibrator(both, 14, fit_before="2026-06-01")
    assert cal is not None and cal.n == 6000
    assert cal.fit_from == cal.fit_to == "2026-05-01"
    assert cal.fit_before == "2026-06-01"
    assert fit_calibrator(both, 14, fit_before="2026-01-01") is None


def test_service_filter_splits_the_fit():
    movies = synthetic(n=6000, seed=42, service="radarr")
    shows = synthetic(n=400, seed=43, service="sonarr")
    both = pd.concat([movies, shows], ignore_index=True)
    big = fit_calibrator(both, 14, service="radarr")
    small = fit_calibrator(both, 14, service="sonarr")
    assert big.service == "radarr" and big.n == 6000 and big.gate_ok is True
    # the thin service still fits — it is simply shrunk much harder
    assert small.service == "sonarr" and small.n == 400 and small.gate_ok is False
    assert shrinkage_weight(small.n_pos) < 0.6 < shrinkage_weight(big.n_pos)
    assert fit_calibrator(both, 14, service="sonarr", require_gate=True) is None


def test_mature_candidates_prefilter_matches_the_maturity_rule():
    df = pd.DataFrame({"snapshot_ts": ["2026-07-01T00:00:00+00:00",
                                       "2026-07-20T00:00:00+00:00",
                                       "not-a-date"]})
    kept = mature_candidates(df, 14, now=pd.Timestamp("2026-07-26T00:00:00+00:00"))
    assert list(kept.index) == [0]           # only the row whose horizon closed


# ── the fitted object ─────────────────────────────────────────────────────────

def test_knots_are_deduplicated_and_monotone():
    cal = fit_calibrator(synthetic(n=6000, seed=42), 14)
    kx = np.asarray(cal.knots_score)
    ky = np.asarray(cal.knots_p)
    assert len(kx) == len(np.unique(kx)) == len(ky)
    assert np.all(np.diff(kx) > 0)
    assert np.all(np.diff(ky) >= -1e-12)
    assert 0.0 <= ky.min() and ky.max() <= 1.0


def test_to_dict_is_json_serialisable():
    cal = fit_calibrator(synthetic(n=6000, seed=42), 14)
    blob = json.loads(json.dumps(cal.to_dict()))
    assert blob["n_pos"] == cal.n_pos and blob["gate_ok"] is True
    assert len(blob["knots_score"]) == len(cal.knots_score)
    assert "knots_score" not in cal.to_dict(with_knots=False)


def test_derive_all_maps_specs_to_services():
    cal = fit_calibrator(synthetic(n=6000, seed=42), 14, service="radarr")
    out = derive_all({"radarr": cal, "sonarr": None}, registry.THRESHOLD_SPECS,
                     registry.DEFAULT_TARGET_P)
    assert set(out) == {s.name for s in registry.THRESHOLD_SPECS}
    assert out["movie_monitor"] is not None
    assert out["series_monitor"] is None        # sonarr had no calibrator


# ── registry: shadow/off are byte-identical ───────────────────────────────────

class _Log:
    def __init__(self):
        self.warnings: list = []

    def log_warning(self, msg):
        self.warnings.append(str(msg))


@pytest.fixture(autouse=True)
def _clean_registry():
    registry.clear()
    yield
    registry.clear()


@pytest.mark.parametrize("mode", ["shadow", "off", None, "typo", ""])
def test_shadow_and_off_return_the_literal_untouched(mode):
    cfg = {} if mode is None else {"ml": {"thresholds": {"mode": mode}}}
    # Prime deliberately WRONG values: they must not leak through.
    registry.prime({s.name: 1.0 for s in registry.THRESHOLD_SPECS})
    for spec in registry.THRESHOLD_SPECS:
        for default in (20, 35.5, 0, 100):
            got = registry.get_threshold(spec.name, cfg, default)
            assert got is default, (spec.name, mode, default)
    assert registry.threshold_mode(cfg) in ("shadow", "off")


def test_unknown_mode_never_arms_derived():
    assert registry.threshold_mode({"ml": {"thresholds": {"mode": "DERVIED"}}}) == "shadow"
    assert registry.threshold_mode({"ml": {"thresholds": {"mode": "DERIVED"}}}) == "derived"
    assert registry.threshold_mode(None) == "shadow"
    assert registry.threshold_mode({}) == "shadow"


def test_derived_mode_uses_the_derived_value():
    cfg = {"ml": {"thresholds": {"mode": "derived"}}}
    registry.prime({"movie_monitor": 27.4, "tv_delete_ceiling": 11.8})
    assert registry.get_threshold("movie_monitor", cfg, 30) == 27       # int in, int out
    assert registry.get_threshold("tv_delete_ceiling", cfg, 20.0) == pytest.approx(11.8)


def test_derived_mode_falls_back_and_warns_when_there_is_no_derived_term():
    cfg = {"ml": {"thresholds": {"mode": "derived"}}}
    log = _Log()
    registry.prime({"movie_monitor": None})
    literal = 30
    got = registry.get_threshold("movie_monitor", cfg, literal, logger=log)
    assert got is literal                 # the caller's OWN object, not a copy
    assert len(log.warnings) == 1
    assert "no derived value" in log.warnings[0]
    assert "shrinkage weight of 0" in log.warnings[0]
    # warn-once: a second read is silent, and still falls back
    assert registry.get_threshold("movie_monitor", cfg, 30, logger=log) == 30
    assert len(log.warnings) == 1


def test_derived_mode_falls_back_and_warns_when_no_report_exists(tmp_path):
    cfg = {"ml": {"thresholds": {"mode": "derived"}}}
    log = _Log()
    assert registry.get_threshold("movie_monitor", cfg, 30, logger=log,
                                  base_dir=tmp_path) == 30
    assert len(log.warnings) == 1 and "no derived value" in log.warnings[0]


def test_derived_mode_loads_the_last_committed_report(tmp_path):
    reports = tmp_path / "ml" / "reports"
    reports.mkdir(parents=True)
    (reports / "thresholds_2026-07-20.json").write_text(json.dumps(
        {"thresholds": [{"name": "movie_monitor", "derived": 11.0, "gate_ok": True}]}),
        encoding="utf-8")
    (reports / "thresholds_2026-07-26.json").write_text(json.dumps(
        {"thresholds": [{"name": "movie_monitor", "derived": 26.0, "gate_ok": True},
                        {"name": "series_demote", "derived": 4.0, "gate_ok": False}]}),
        encoding="utf-8")
    cfg = {"ml": {"thresholds": {"mode": "derived"}}}
    assert registry.get_threshold("movie_monitor", cfg, 30, base_dir=tmp_path) == 26
    assert registry.get_threshold("series_demote", cfg, 20, base_dir=tmp_path) == 20
    assert registry.latest_report_path(tmp_path).name == "thresholds_2026-07-26.json"


def test_bool_defaults_are_never_coerced():
    cfg = {"ml": {"thresholds": {"mode": "derived"}}}
    registry.prime({"movie_monitor": 27.4})
    assert registry.get_threshold("movie_monitor", cfg, True) is True


# ── registry: config surface ──────────────────────────────────────────────────

def _real_shaped_calibrator() -> CalibrationResult:
    """A stand-in for THIS repo's measured calibrator (H=14d, radarr, n=11443,
    n_pos=38): a step function on integer scores with seven blocks, flat above
    31 because the highest matured score is 56. Reproduced from the real fit —
    see derive.py's module docstring for the command that prints it."""
    blocks = ((0, 6, 0.0), (7, 11, 0.002018), (12, 20, 0.002322),
              (21, 23, 0.007282), (24, 29, 0.007958), (30, 30, 0.029851),
              (31, 56, 0.037815))
    xs, ps = [], []
    for lo, hi, p in blocks:
        for s in range(lo, hi + 1):
            xs.append(float(s))
            ps.append(p)
    return CalibrationResult(knots_score=tuple(xs), knots_p=tuple(ps))


def test_default_targets_reproduce_todays_constants_on_todays_calibrator():
    """The inversion that CHOSE the defaults, replayed on the real fit's shape.
    Each default must (a) be reachable, and (b) select exactly the scores whose
    calibrated probability meets it — including the hand-set constant itself."""
    cal = _real_shaped_calibrator()
    t = registry.DEFAULT_TARGET_P
    assert cal.p_at(20) == pytest.approx(0.002322, abs=1e-6)
    assert cal.p_at(30) == pytest.approx(0.029851, abs=1e-6)
    assert cal.p_at(35) == pytest.approx(0.037815, abs=1e-6)
    assert cal.p_at(70) == pytest.approx(0.037815, abs=1e-6)   # clamped, saturated
    for bucket, p in t.items():
        assert p <= cal.p_max, bucket                          # never rounded up
        assert derive_threshold(cal, p) < 100.0, bucket
    for bucket, constant in (("delete", 20), ("monitor", 30), ("acquire", 35),
                             ("uhd", 70)):
        # the constant still passes its own derived rule …
        assert cal.p_at(constant) >= t[bucket] - 1e-12, bucket
        # … and the derived cutoff is the LEFT EDGE of the constant's plateau:
        # the first integer score it admits carries the constant's probability.
        derived = derive_threshold(cal, t[bucket])
        assert derived <= constant + 1e-9, bucket
        assert cal.p_at(math.ceil(derived)) == pytest.approx(
            cal.p_at(constant), abs=1e-12), bucket
    # the documented, measured values
    assert derive_threshold(cal, t["delete"]) == pytest.approx(11.93, abs=0.05)
    assert derive_threshold(cal, t["monitor"]) == pytest.approx(29.99, abs=0.05)
    assert derive_threshold(cal, t["acquire"]) == pytest.approx(31.0, abs=0.05)
    assert derive_threshold(cal, t["uhd"]) == pytest.approx(31.0, abs=0.05)


def test_rounding_a_target_up_past_the_plateau_would_clamp_to_100():
    """Why the defaults are TRUNCATED: 0.037815 -> 0.038 names odds no score on
    this calibrator reaches, and the inverse then selects nothing."""
    cal = _real_shaped_calibrator()
    assert derive_threshold(cal, 0.038) == 100.0
    assert derive_threshold(cal, 0.0378) == pytest.approx(31.0, abs=0.05)


def test_target_probabilities_override_and_reject_nonsense():
    cfg = {"ml": {"thresholds": {"targets": {
        "acquire": 0.25, "monitor": "nope", "delete": -1, "uhd": 2.0}}}}
    got = registry.target_probabilities(cfg)
    assert got["acquire"] == 0.25
    assert got["monitor"] == registry.DEFAULT_TARGET_P["monitor"]
    assert got["delete"] == registry.DEFAULT_TARGET_P["delete"]
    assert got["uhd"] == registry.DEFAULT_TARGET_P["uhd"]
    assert registry.target_probabilities(None) == registry.DEFAULT_TARGET_P


def test_shrinkage_k_config_surface():
    assert registry.DEFAULT_SHRINKAGE_K == 150.0
    assert registry.shrinkage_k({}) == 150.0
    assert registry.shrinkage_k({"ml": {"thresholds": {"shrinkage_k": 40}}}) == 40.0
    # a k that would disable shrinkage is refused: 0/negative/garbage -> default
    for bad in (0, -1, "nope", None, [1]):
        assert registry.shrinkage_k({"ml": {"thresholds": {"shrinkage_k": bad}}}) == 150.0
    # the documented choice, against §10's ladder
    assert GATE_MILESTONES[0] < registry.DEFAULT_SHRINKAGE_K < GATE_MILESTONES[1]


def test_include_backfill_defaults_true_for_thresholds_only():
    """The ONE ML entry point that opts in by default — a fresh install has no
    other labels. The offline validators keep their own (false) default."""
    assert registry.DEFAULT_INCLUDE_BACKFILL is True
    assert registry.include_backfill({}) is True
    assert registry.include_backfill(None) is True
    assert registry.include_backfill(
        {"ml": {"thresholds": {"include_backfill": False}}}) is False
    assert registry.include_backfill(
        {"ml": {"thresholds": {"include_backfill": "no"}}}) is False
    assert registry.include_backfill(
        {"ml": {"thresholds": {"include_backfill": "yes"}}}) is True


def test_top_level_thresholds_block_is_accepted_as_an_alias():
    assert registry.threshold_mode({"thresholds": {"mode": "off"}}) == "off"
    assert registry.horizon_days({"thresholds": {"horizon_days": 7}}) == 7
    assert registry.horizon_days({}) == 14
    assert registry.horizon_days({"ml": {"thresholds": {"horizon_days": 0}}}) == 14


def test_resolve_constant_matches_the_consumer():
    for spec in registry.THRESHOLD_SPECS:
        assert registry.resolve_constant(spec, {}) == float(spec.constant)
    monitor = registry.SPEC_BY_NAME["movie_monitor"]
    assert registry.resolve_constant(monitor, {"owned_monitor_score_threshold": 44}) == 44.0
    # dotted key
    pilot = registry.SPEC_BY_NAME["pilot_min_watchability"]
    assert registry.resolve_constant(
        pilot, {"pilot_interactive": {"min_watchability": 8}}) == 8.0
    # `cfg or DEFAULT` consumers: 0 means unset, not zero
    uhd = registry.SPEC_BY_NAME["uhd_dual"]
    assert uhd.falsy_means_default is True
    assert registry.resolve_constant(uhd, {"routing": {"movies": {"4k_dual_min_score": 0}}}) == 75.0
    assert registry.resolve_constant(uhd, {"routing": {"movies": {"4k_dual_min_score": 40}}}) == 40.0


def test_report_enabled_follows_mode_and_the_kill_switch(monkeypatch):
    assert registry.report_enabled({}) is True
    assert registry.report_enabled({"ml": {"thresholds": {"mode": "off"}}}) is False
    monkeypatch.setenv("GLIDEARR_THRESHOLDS_OFF", "1")
    assert registry.report_enabled({}) is False


# ── the inventory is honest ───────────────────────────────────────────────────

def test_every_spec_is_well_formed():
    names = [s.name for s in registry.THRESHOLD_SPECS]
    assert len(names) == len(set(names))
    for s in registry.THRESHOLD_SPECS:
        assert s.bucket in registry.BUCKETS, s.name
        assert s.service in ("radarr", "sonarr"), s.name
        assert 0 <= s.constant <= 100, s.name
        assert ":" in s.consumer, s.name
        if not s.routed:
            assert s.note, f"{s.name}: an unrouted threshold must say why"


def test_every_routed_spec_has_a_live_call_site():
    """A routed name that no consumer passes is a silent no-op — catch the typo
    here rather than discovering it the first time mode='derived' is flipped."""
    src = "\n".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in (_REPO_ROOT / "scripts" / "managers" / "services").rglob("*.py")
        if not p.name.startswith("test_"))
    for spec in registry.ROUTED_SPECS:
        assert f'get_threshold("{spec.name}"' in src, spec.name
    for spec in registry.THRESHOLD_SPECS:
        if not spec.routed:
            assert f'get_threshold("{spec.name}"' not in src, spec.name


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
