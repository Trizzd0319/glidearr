"""Tests for foundation/ — every canonical formula against hand-computed cases,
planted-parameter recovery, and equivalence with the operative implementations
it delegates to (ranker key, coordinator downgrade arithmetic, survival model).
Fixed seeds throughout. Runnable both ways:

    python -m pytest scripts/managers/machine_learning/foundation/test_foundation.py
    python scripts/managers/machine_learning/foundation/test_foundation.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np
import pandas as pd

from scripts.managers.machine_learning.foundation import (
    average_precision,
    brier_score,
    discrete_hazard,
    downgrade_credit,
    empirical_bayes_pool,
    expected_calibration_error,
    fit_logistic_irls,
    isotonic_calibrate,
    isotonic_fit,
    linear_utility,
    logistic_probability,
    residual_watch_probability,
    spearman_rho,
    standardize,
    suggested_weight_multipliers,
    value_density,
)
from scripts.managers.machine_learning.likelihood import survival
from scripts.managers.machine_learning.space.coordinator_ranker import (
    critic_sort,
    select_for_target,
    utility_per_gb,
)


# ── production model form ───────────────────────────────────────────────────────

def test_linear_utility():
    # dict form, implicit unit weights (the production setting)
    assert linear_utility({"A1": 15.0, "F1": 20.0, "G2": -10.0}) == 25.0
    # explicit weights; a missing key defaults to 1.0
    assert linear_utility({"A1": 10.0, "B1": 4.0}, {"A1": 0.5}) == 9.0
    # sequence form is a plain dot product
    assert linear_utility([1.0, 2.0, 3.0], [1.0, 0.0, 2.0]) == 7.0
    assert linear_utility([1.0, 2.0, 3.0]) == 6.0


# ── logistic refit ──────────────────────────────────────────────────────────────

def test_standardize():
    x = np.array([[1.0, 5.0], [3.0, 5.0], [5.0, 5.0]])
    z, mu, sd = standardize(x)
    assert np.allclose(mu, [3.0, 5.0]) and np.allclose(sd[0], np.std([1, 3, 5]))
    assert np.allclose(z[:, 0].mean(), 0.0) and np.allclose(z[:, 0].std(), 1.0)
    assert np.all(z[:, 1] == 0.0)                      # zero-variance column -> 0
    # applying TRAIN moments to a test window
    z2, _, _ = standardize(np.array([[5.0, 5.0]]), mu=mu, sd=sd)
    assert np.allclose(z2[0, 0], (5.0 - 3.0) / sd[0]) and z2[0, 1] == 0.0


def test_irls_recovers_planted_coefficients():
    rng = np.random.default_rng(42)
    n, beta_true, icpt_true = 4000, np.array([1.5, -1.0, 0.5]), -0.5
    X = rng.standard_normal((n, 3))
    p = 1.0 / (1.0 + np.exp(-(X @ beta_true + icpt_true)))
    y = (rng.random(n) < p).astype(float)
    beta, icpt, converged = fit_logistic_irls(X, y, l2=1e-3)
    assert converged
    assert np.all(np.abs(beta - beta_true) < 0.15), beta
    assert abs(icpt - icpt_true) < 0.15, icpt
    # AUC sanity: the fitted ranking must beat the prevalence floor decisively
    p_hat = logistic_probability(X, beta, icpt)
    ap = average_precision(y, p_hat)
    prevalence = float(y.mean())
    assert ap > prevalence + 0.15, (ap, prevalence)
    # zero-variance-column equivalence claimed by standardize's docstring:
    # an all-zero column earns beta ~ 0 and leaves the others untouched
    Xz = np.hstack([X, np.zeros((n, 1))])
    beta4, icpt4, _ = fit_logistic_irls(Xz, y, l2=1e-3)
    assert abs(beta4[3]) < 1e-9
    assert np.allclose(beta4[:3], beta, atol=1e-9) and abs(icpt4 - icpt) < 1e-9


def test_logistic_probability():
    assert float(logistic_probability(np.zeros((1, 2)), [1.0, 1.0], 0.0)[0]) == 0.5
    # eta clip at +-30 saturates without overflow
    hi = float(logistic_probability(np.array([[100.0]]), [1.0], 0.0)[0])
    assert 1.0 - hi < 1e-12


def test_suggested_weight_multipliers():
    m = suggested_weight_multipliers([2.0, -1.0, 0.5])
    assert np.allclose(m, [1.0, -0.5, 0.25])
    assert np.all(suggested_weight_multipliers([0.0, 0.0]) == 0.0)
    assert suggested_weight_multipliers([]).size == 0
    # alignment with ml_weight_refit's expression: m = w/denom if denom>0 else 0
    per_point = np.array([0.03, -0.12, 0.06])
    denom = float(np.max(np.abs(per_point)))
    expect = np.array([w / denom if denom > 0 else 0.0 for w in per_point])
    assert np.allclose(suggested_weight_multipliers(per_point), expect)


# ── evaluation metrics ──────────────────────────────────────────────────────────

def test_average_precision_hand_case():
    # ranked desc: y = [1,0,1,0,0] -> precision at the positives = 1/1, 2/3
    y = [1, 0, 1, 0, 0]
    s = [0.9, 0.8, 0.7, 0.6, 0.5]
    assert abs(average_precision(y, s) - (1.0 + 2.0 / 3.0) / 2.0) < 1e-12
    # perfect ranking -> 1.0; no positives -> NaN
    assert average_precision([0, 1], [0.1, 0.9]) == 1.0
    assert np.isnan(average_precision([0, 0], [0.1, 0.9]))


def test_brier_hand_case():
    # ((0.2)^2 + (0.2)^2 + (0.5)^2) / 3 = 0.11
    assert abs(brier_score([1, 0, 1], [0.8, 0.2, 0.5]) - 0.11) < 1e-12
    assert brier_score([1, 0], [1.0, 0.0]) == 0.0


def test_ece_hand_case():
    # bins=2: both bins hold rate 0.5 vs mean-pred 0.1 / 0.9 -> 0.5*0.4 + 0.5*0.4
    y = [1, 0, 0, 1]
    p = [0.9, 0.9, 0.1, 0.1]
    assert abs(expected_calibration_error(y, p, bins=2) - 0.4) < 1e-12
    # perfectly calibrated corners -> 0
    assert expected_calibration_error([0, 1], [0.0, 1.0], bins=2) == 0.0
    # empty input -> NaN
    assert np.isnan(expected_calibration_error([], []))


def test_spearman():
    assert abs(spearman_rho([1, 2, 3, 4], [10, 20, 30, 40]) - 1.0) < 1e-12
    assert abs(spearman_rho([1, 2, 3, 4], [40, 30, 20, 10]) + 1.0) < 1e-12
    assert np.isnan(spearman_rho([1, 1, 1], [1, 2, 3]))    # constant -> undefined


# ── isotonic calibration ────────────────────────────────────────────────────────

def test_isotonic_monotone_and_idempotent():
    x = [1.0, 2.0, 3.0, 4.0]
    y = [1.0, 0.0, 3.0, 2.0]
    bx, by = isotonic_fit(x, y)
    assert np.allclose(by, [0.5, 0.5, 2.5, 2.5])            # pooled block means
    assert np.all(np.diff(by) >= -1e-12)                     # monotone
    _, by2 = isotonic_fit(bx, by)                            # idempotent
    assert np.allclose(by2, by)
    # calibrate: training points reproduce the fit; ends clamp flat
    fitted = isotonic_calibrate(x, y)
    assert np.allclose(fitted, by)
    ends = isotonic_calibrate(x, y, eval_scores=[-10.0, 10.0])
    assert np.allclose(ends, [0.5, 2.5])
    # binary outcomes -> calibrated probabilities in [0,1]
    p = isotonic_calibrate([1, 2, 3, 4, 5, 6], [0, 0, 1, 0, 1, 1])
    assert np.all((p >= 0.0) & (p <= 1.0)) and np.all(np.diff(p) >= -1e-12)


# ── survival: hazard / residual probability / EB pooling ────────────────────────

def test_hazard_hand_case():
    # bucket_days=7, max_days=21 -> 3 buckets. complete gaps 3,10,15; censored 8.
    hz, at_risk = discrete_hazard([3.0, 10.0, 15.0], [8.0], bucket_days=7, max_days=21)
    assert np.allclose(at_risk, [4.0, 3.0, 1.0])
    assert np.allclose(hz, [1.0 / 4.0, 1.0 / 3.0, 1.0])
    # residual over 2 whole buckets: 1 - (1-.25)(1-1/3) = 0.5
    assert abs(residual_watch_probability(hz, 0.0, 14.0, bucket_days=7) - 0.5) < 1e-12
    # partial bucket: half of bucket 0 -> 1 - (1 - .25*.5) = 0.125
    assert abs(residual_watch_probability(hz, 0.0, 3.5, bucket_days=7) - 0.125) < 1e-12
    # beyond the curve the last hazard extends flat (h=1 -> certainty)
    assert residual_watch_probability(hz, 21.0, 7.0, bucket_days=7) == 1.0
    # degenerate windows
    assert residual_watch_probability(hz, 0.0, 0.0, bucket_days=7) == 0.0
    assert residual_watch_probability(np.array([]), 0.0, 14.0, bucket_days=7) == 0.0


def test_empirical_bayes_pool_limits():
    parent = np.array([0.5, 0.5])
    child = np.array([0.1, 0.9])
    # n=0 -> group/parent value exactly
    assert np.allclose(empirical_bayes_pool(child, 0.0, parent, k=5.0), parent)
    assert np.allclose(empirical_bayes_pool(None, 3.0, parent, k=5.0), parent)
    # n=k -> exact midpoint
    assert np.allclose(empirical_bayes_pool(child, 5.0, parent, k=5.0), [0.3, 0.7])
    # n -> inf: the title speaks for itself
    assert np.allclose(empirical_bayes_pool(child, 1e9, parent, k=5.0), child, atol=1e-6)
    # scalar ride-through
    assert abs(float(empirical_bayes_pool(0.2, 5.0, 0.6, k=5.0)) - 0.4) < 1e-12
    # Beta-Binomial identity from the docstring: (d + k*hbar)/(n + k) == blend
    d, n_ar, hbar, k = 3.0, 10.0, 0.2, 5.0
    posterior = (d + k * hbar) / (n_ar + k)
    blended = float(empirical_bayes_pool(d / n_ar, n_ar, hbar, k=k))
    assert abs(posterior - blended) < 1e-12


def test_survival_model_parity():
    """foundation's delegating forms reproduce the SurvivalModel numbers exactly
    on synthetic fixed-seed events (the ml_survival_report computation chain)."""
    rng = np.random.default_rng(11)
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events_by_entity: dict = {}
    for i, (n_ev, scale) in enumerate([(30, 8.0), (12, 20.0), (5, 30.0),
                                       (3, 45.0), (2, 15.0), (8, 10.0)]):
        t = base + timedelta(days=float(rng.uniform(0, 30)))
        ev = []
        for _ in range(n_ev):
            ev.append(t)
            t = t + timedelta(days=float(rng.exponential(scale)) + 0.25)
        # mix timestamp encodings to exercise the coercion path
        events_by_entity[("movie", 100 + i)] = [
            e.isoformat() if j % 3 == 0 else e for j, e in enumerate(ev)]
    groups = {("movie", 100): "collection:alpha", ("movie", 101): "collection:alpha",
              ("movie", 103): "collection:beta"}
    model = survival.fit_survival_model(events_by_entity, now=now,
                                        groups_by_entity=groups)

    # household curve == foundation.discrete_hazard on the pooled gaps
    all_gaps: list = []
    all_cens: list = []
    for ev in events_by_entity.values():
        g, c, _ = survival.entity_gaps(ev, now)
        all_gaps.extend(g)
        all_cens.append(c)
    hz, _ = discrete_hazard(all_gaps, all_cens)
    assert np.array_equal(hz, model.household)

    for entity in events_by_entity:
        # curve_for == the two-level empirical_bayes_pool composition
        grp = model.group_of.get(entity)
        g = model.groups.get(grp)
        pooled = empirical_bayes_pool(g[0] if g else None, g[1] if g else 0.0,
                                      model.household, k=model.k)
        t = model.titles.get(entity)
        blended = empirical_bayes_pool(t[0] if t else None, t[1] if t else 0.0,
                                       pooled, k=model.k)
        assert np.array_equal(blended, model.curve_for(entity)), entity
        # residual probabilities identical through the foundation form
        for gap in (0.0, 10.0, 40.0, 200.0):
            a = model.residual_watch_probability(entity, gap, 14.0)
            b = residual_watch_probability(model.curve_for(entity), gap, 14.0,
                                           bucket_days=model.bucket_days)
            assert a == b, (entity, gap)


# ── space decision theory ───────────────────────────────────────────────────────

def test_value_density_edges_and_ranker_match():
    assert value_density(10.0, 0.0) == 100.0        # zero-GB clamp -> /0.1
    assert value_density(10.0, 0.05) == 100.0       # sub-floor clamps identically
    assert value_density(10.0, None) == 100.0       # missing size -> 0.0 -> clamp
    assert value_density(7.5, 2.5) == 3.0
    assert value_density(0.0, 50.0) == 0.0
    # exact key match against the ranker on sample candidates
    cands = [{"score": 8, "size_gb": 2.0}, {"score": 2, "size_gb": 40.0},
             {"score": 55, "size_gb": 0.0}, {"score": 12, "size_gb": None},
             {"score": 3.5, "size_gb": 0.09}]
    for c in cands:
        assert utility_per_gb(c, float(c["score"])) == \
            value_density(c["score"], c["size_gb"]), c


def test_value_density_matches_utility_mode_ordering():
    pool = [
        {"score": 90, "size_gb": 60.0, "critic": 8.0, "fid": "big_good"},
        {"score": 10, "size_gb": 50.0, "critic": 3.0, "fid": "big_bad"},
        {"score": 10, "size_gb": 1.0, "critic": 6.0, "fid": "small_bad"},
        {"score": 40, "size_gb": 0.0, "critic": None, "fid": "zero_gb"},
        {"score": 5, "size_gb": 25.0, "critic": None, "fid": "cold_mid"},
    ]
    sel, _ = select_for_target([dict(c) for c in pool], need_gb=1e9,
                               ranking_mode="utility_per_gb")
    expect = sorted(pool, key=lambda c: (value_density(c["score"], c["size_gb"]),
                                         critic_sort(c.get("critic")),
                                         -float(c.get("size_gb") or 0.0)))
    assert [c["fid"] for c in sel] == [c["fid"] for c in expect]


def test_downgrade_credit_edges():
    assert downgrade_credit(100.0, [50.0], 1.0) == 50.0
    assert downgrade_credit(100.0, [50.0], 2.0) == 50.0      # ratio clamps to 1
    assert downgrade_credit(100.0, [50.0], -0.5) == 100.0    # ratio clamps to 0
    assert downgrade_credit(100.0, [-30.0, 40.0], 1.0) == 60.0  # negative ignored
    assert downgrade_credit(100.0, [80.0, 50.0], 1.0) == 0.0    # over-cover floors at 0
    assert downgrade_credit(0.0, [50.0], 1.0) == 0.0
    assert downgrade_credit(100.0, [float("nan"), 40.0], 1.0) == 60.0
    assert downgrade_credit(100.0, 30.0, 1.0) == 70.0        # scalar reclaim
    assert downgrade_credit(100.0, [], 1.0) == 100.0
    assert downgrade_credit(100.0, [25.0], 0.5) == 87.5


def test_downgrade_credit_mirrors_coordinator_arithmetic():
    """Replays the space_coordinator sequence: need = max(0, U - free); ratio
    clamped to [0,1]; _projected_downgrade_gb sums positive plan_reclaim_gb
    (pd.to_numeric coerce + > 0); credit = min(need, dg*ratio); need -= credit."""
    def coordinator_need_after(u, free, reclaims, ratio_cfg):
        need = max(0.0, u - free)
        try:
            r = float(ratio_cfg)
        except (TypeError, ValueError):
            r = 1.0
        r = min(max(r, 0.0), 1.0)
        vals = pd.to_numeric(pd.Series(list(reclaims), dtype=object), errors="coerce")
        dg = float(vals[vals > 0].sum()) if r > 0 else 0.0
        credit = min(need, dg * r)
        return need - credit

    cases = [
        (500.0, 400.0, [10.0, 25.5, -4.0], 1.0),
        (500.0, 400.0, [10.0, 25.5, -4.0], 0.5),
        (500.0, 400.0, [200.0], 1.0),                 # credit caps at need
        (500.0, 650.0, [50.0], 1.0),                  # free > U -> need 0
        (500.0, 400.0, [], 1.0),
        (500.0, 400.0, [float("nan"), 30.0], 1.0),
        (500.0, 400.0, [30.0], 7.5),                  # ratio clamps high
        (500.0, 400.0, [30.0], -1.0),                 # ratio clamps low
        (500.0, 400.0, [0.0, 12.25], 0.33),
    ]
    for u, free, reclaims, ratio in cases:
        expect = coordinator_need_after(u, free, reclaims, ratio)
        got = downgrade_credit(max(0.0, u - free), reclaims, ratio)
        assert abs(got - expect) < 1e-12, (u, free, reclaims, ratio, got, expect)


# ── standalone runner ───────────────────────────────────────────────────────────

_GROUPS = [
    ("linear_utility", [test_linear_utility]),
    ("standardize/logistic/IRLS", [test_standardize,
                                   test_irls_recovers_planted_coefficients,
                                   test_logistic_probability]),
    ("weight multipliers", [test_suggested_weight_multipliers]),
    ("average_precision", [test_average_precision_hand_case]),
    ("brier/ECE", [test_brier_hand_case, test_ece_hand_case]),
    ("spearman", [test_spearman]),
    ("isotonic/PAVA", [test_isotonic_monotone_and_idempotent]),
    ("hazard/residual", [test_hazard_hand_case]),
    ("EB pooling", [test_empirical_bayes_pool_limits]),
    ("survival model parity", [test_survival_model_parity]),
    ("value_density", [test_value_density_edges_and_ranker_match,
                       test_value_density_matches_utility_mode_ordering]),
    ("downgrade_credit", [test_downgrade_credit_edges,
                          test_downgrade_credit_mirrors_coordinator_arithmetic]),
]

if __name__ == "__main__":
    failed = 0
    for name, fns in _GROUPS:
        try:
            for fn in fns:
                fn()
            print(f"PASS {name} ({len(fns)} test{'s' if len(fns) > 1 else ''})")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {name}: {e!r}")
    if failed:
        raise SystemExit(f"{failed} group(s) failed")
    print(f"All {sum(len(f) for _, f in _GROUPS)} tests in {len(_GROUPS)} groups passed.")
