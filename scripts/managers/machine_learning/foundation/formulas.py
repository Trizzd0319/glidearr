"""
foundation/formulas.py — canonical definitions of every statistical formula.
================================================================================
One rigorous docstring per formula: the math, why it is the right estimator at
this data regime, and where the codebase uses it. Every function here DELEGATES
to the operative implementation (eval/np_metrics, likelihood/survival,
space/coordinator_ranker) — the math is defined once and wrapped, never
duplicated, so foundation can NEVER drift from what the tools/planners compute.
The full derivations live in ../MATH_FOUNDATION.md; docstrings here are the
executable index into that document.

PURE — numpy/stdlib over plain arrays; no I/O, no manager graph, no clock.
Nothing at runtime imports this package; it exists so adopters (new tools,
future policy derivations) import formulas instead of re-implementing them.
"""
from __future__ import annotations

import numpy as np

from scripts.managers.machine_learning.eval.np_metrics import (
    average_precision as _np_average_precision,
    brier_score as _np_brier_score,
    calibration_table,                    # noqa: F401  (re-exported via __init__)
    expected_calibration_error as _np_expected_calibration_error,
    isotonic_fit,
    isotonic_predict,
    logistic_irls as _np_logistic_irls,
    minmax_scale,                         # noqa: F401  (re-exported via __init__)
    spearman_rho as _np_spearman_rho,
)
from scripts.managers.machine_learning.likelihood.survival import (
    DEFAULT_BUCKET_DAYS,
    DEFAULT_K,
    DEFAULT_MAX_DAYS,
    blend_hazards as _sv_blend_hazards,
    entity_gaps,                          # noqa: F401  (re-exported via __init__)
    hazard_curve as _sv_hazard_curve,
    residual_probability_from_hazard as _sv_residual_probability,
)
from scripts.managers.machine_learning.space.coordinator_ranker import (
    utility_per_gb as _ranker_utility_per_gb,
)


# ── 1. the production model ───────────────────────────────────────────────────

def linear_utility(features, weights=None) -> float:
    """Hand-weighted linear utility:  s(x) = Σ_g w_g · f_g(x).

    THE form of the production watchability score. Each signal group g
    (A1..G4) emits an independently-CAPPED contribution f_g(x); the score is
    their plain sum with implicit unit weights (w_g = 1 today, hand-edited
    only). Capping bounds any single group's influence — the linear model's
    defense against one noisy signal dominating — and makes every score
    decomposable into an additive, human-auditable breakdown. The production
    scorers then clamp: s* = max(0, min(100, round(s))).

    OPERATIVE IMPLEMENTATIONS (this function documents the form; do NOT rewire
    them): scoring/movie_scorer.score_movie and scoring/show_scorer.score_show
    compute the f_g terms and this sum inline (breakdown["_total_raw"]);
    features/movie_features.py + features/show_features.py marshal cache rows
    into their arguments. The Stage-1 snapshots persist the f_g terms as
    sig_* columns, which is what makes the refit (fit_logistic_irls) possible.

    ``features``: mapping {group: contribution} or a sequence of contributions.
    ``weights``:  matching mapping/sequence; None → all 1.0 (the production
    setting). A mapping missing a key defaults that weight to 1.0. Returns the
    raw (pre-clamp) sum as float."""
    if hasattr(features, "keys"):
        if weights is None:
            return float(sum(float(v) for v in features.values()))
        return float(sum(float(v) * float(weights.get(k, 1.0))
                         for k, v in features.items()))
    f = np.asarray(list(features), dtype=float)
    if weights is None:
        return float(f.sum())
    w = np.asarray(list(weights), dtype=float)
    return float(f @ w)


# ── 2. logistic refit (Stage 3) ───────────────────────────────────────────────

def standardize(x, mu=None, sd=None) -> "tuple[np.ndarray, np.ndarray, np.ndarray]":
    """Column z-scores:  z_j = (x_j − μ_j) / σ_j.

    Puts heterogeneous signal columns (a ±12 completion bump vs a +2 recency
    bump) on one scale so the L2 penalty in fit_logistic_irls shrinks them
    comparably — un-standardized, ridge would punish large-range columns
    hardest for a unit change in log-odds. μ/σ are the population moments
    (numpy default, ddof=0), computed here when not supplied; supply TRAIN
    moments to transform a test window (fitting them on test leaks the future
    into the past — see MATH_FOUNDATION §3). A zero-variance column
    (σ ≤ 1e-12) maps to z = 0.0 everywhere: under ridge (λ > 0) an all-zero
    column earns exactly β = 0 and leaves every other coefficient untouched,
    so this is fit-equivalent to ml_weight_refit's policy of DROPPING
    zero-variance columns before fitting.

    USED BY: scripts/support/tools/ml_weight_refit.py (train-window moments,
    zero-variance drop). Returns (z, mu, sd) with x coerced to 2-D (n, d)."""
    xm = np.asarray(x, dtype=float)
    if xm.ndim == 1:
        xm = xm[:, None]
    mu = xm.mean(axis=0) if mu is None else np.asarray(mu, dtype=float)
    sd = xm.std(axis=0) if sd is None else np.asarray(sd, dtype=float)
    safe = np.where(sd > 1e-12, sd, 1.0)
    z = (xm - mu) / safe
    z[:, sd <= 1e-12] = 0.0
    return z, mu, sd


def logistic_probability(z, beta, intercept: float = 0.0) -> np.ndarray:
    """Logistic response:  P(y=1 | z) = σ(β₀ + Σ_j β_j z_j),  σ(η) = 1/(1+e^−η).

    The inverse-logit link mapping a linear score to a probability — the model
    class of the Stage-3 refit. σ is the canonical link for Bernoulli outcomes:
    the log-likelihood is concave in β, so IRLS/Newton finds the global
    optimum. η is clipped to ±30 (σ saturates to within ~1e-13 of {0,1} there),
    matching the overflow guard inside eval/np_metrics.logistic_irls so
    predictions round-trip the fit exactly.

    USED BY: the refit model form (ml_weight_refit reports per-point log-odds
    β_j/σ_j from this model); challenger calibration targets the same
    probability scale. Returns σ(η) as an ndarray (scalar-shaped for 1-D z
    with one row)."""
    zm = np.asarray(z, dtype=float)
    b = np.asarray(beta, dtype=float).ravel()
    eta = (zm @ b if zm.ndim > 1 else float(zm @ b)) + float(intercept)
    eta = np.clip(eta, -30, 30)
    return 1.0 / (1.0 + np.exp(-eta))


def fit_logistic_irls(X, y, l2: float = 1.0, max_iter: int = 50,
                      tol: float = 1e-8) -> "tuple[np.ndarray, float, bool]":
    """L2-penalized logistic regression via IRLS (delegates to np_metrics).

    Maximizes the penalized log-likelihood
        ℓ(β) = Σ_i [y_i log p_i + (1−y_i) log(1−p_i)]  −  (λ/2)‖β₋₀‖²
    (intercept unpenalized). Newton step with W = diag(p_i(1−p_i)):
        β ← β + (XᵀWX + λI₋₀)⁻¹ (Xᵀ(y − p) − λβ₋₀)
    — algebraically the weighted-least-squares form (XᵀWX+λI)⁻¹XᵀW z̃ with
    working response z̃ = Xβ + W⁻¹(y − p), hence "iteratively reweighted least
    squares". The ridge term keeps XᵀWX + λI invertible under the separable /
    rarely-firing signal columns this dataset actually has (a group that fired
    3 times can be perfectly separated by chance); it biases β toward 0 with
    variance reduced by ~(1+λ/eig)⁻¹ per eigendirection — the right trade at
    n_pos ≪ 100. MLE properties (consistency, asymptotic normality) hold as
    λ/n → 0; at our n the estimates are honest but HIGH-VARIANCE, which is why
    Stage 3 only ever prints suggestions.

    USED BY: scripts/support/tools/ml_weight_refit.py (the Stage-3 core).
    Caller standardizes X (see standardize); y in {0,1}. Returns
    (beta, intercept, converged)."""
    return _np_logistic_irls(X, y, l2=l2, max_iter=max_iter, tol=tol)


def suggested_weight_multipliers(betas) -> np.ndarray:
    """Refit multiplier normalization:  m_g = β_g / max_k |β_k|.

    Scales the fitted per-group evidence so the strongest group reads 1.0 —
    directly comparable to today's implicit w_g = 1 per group. |m_g| is the
    group's evidence relative to the best; m_g < 0 means its points correlate
    with NOT watching. Scale-free: multiplying all β by c > 0 leaves m
    unchanged, so multipliers are comparable across refits with different λ or
    n. All-zero (or empty, or non-finite-max) input → all-zero multipliers,
    matching ml_weight_refit's ``denom > 0`` guard.

    USED BY: scripts/support/tools/ml_weight_refit.py, which applies this to
    the PER-POINT weights w_j = β_j/σ_j (log-odds per raw score point) — pass
    per-point weights to reproduce its ``fitted_multiplier`` column exactly.
    Suggestions only; nothing applies them (the scorers stay hand-edited)."""
    b = np.asarray(betas, dtype=float).ravel()
    denom = float(np.max(np.abs(b))) if b.size else 0.0
    if not (denom > 0):
        return np.zeros_like(b)
    return b / denom


# ── 3. evaluation metrics (Stage 2/4) ─────────────────────────────────────────

def average_precision(y_true, y_score) -> float:
    """Average precision (AUC-PR):  AP = (1/P) Σ_k precision@k · 1[y_(k) = 1].

    Sum of precision at each positive's rank, over the P positives — the
    step-function area under the precision-recall curve (sklearn-compatible;
    delegation target sorts stably, descending score). Under this dataset's
    ~2% positive prevalence, ROC-AUC is dominated by true-negative ordering
    and barely moves when the top of the ranking changes; AP weights exactly
    the region the system acts on (what gets kept/acquired first). Baseline
    for a random ranking ≈ prevalence, so AP is read against that floor, not
    against 0.5. NaN with no positives — undefined, and the tools SAY so.

    USED BY: ml_forward_validation (score-as-ranker), ml_weight_refit
    (current vs refit ranking), ml_train_challenger + challenger/gbt_shadow
    (train/valid metrics), eval/forward. Delegates to
    eval/np_metrics.average_precision."""
    return _np_average_precision(y_true, y_score)


def brier_score(y_true, p) -> float:
    """Brier score:  BS = (1/N) Σ_i (p_i − y_i)².

    Mean squared error of probability forecasts — a strictly proper scoring
    rule (uniquely minimized in expectation by the true probability, so it
    cannot be gamed by hedging). Murphy decomposition:
        BS = reliability − resolution + uncertainty
    i.e. calibration error minus refinement plus irreducible outcome variance
    ȳ(1−ȳ) — a forecaster improves BS only via calibration or sharpness. At
    ~2% prevalence the all-zeros forecast scores BS ≈ 0.02; always compare
    against that floor, not 0.

    USED BY: ml_forward_validation (on the min-max-scaled score — the score is
    NOT a probability, so its Brier is a calibration diagnostic, not a loss),
    challenger training metrics. Delegates to eval/np_metrics.brier_score."""
    return _np_brier_score(y_true, p)


def expected_calibration_error(y_true, p, bins: int = 10) -> float:
    """ECE = Σ_b (n_b / N) · |ȳ_b − p̄_b|  over equal-width probability bins.

    The bin-weighted mean absolute gap between claimed probability (p̄_b) and
    realized rate (ȳ_b) — the single-number summary of the reliability
    diagram that eval/np_metrics.calibration_table prints per-bin. It is the
    "reliability" term of the Brier decomposition estimated by binning, in
    probability units (ECE 0.05 = forecasts off by 5 points on average).
    Binning bias: with few samples per bin the estimate is noisy and
    ECE → 0 artifacts appear when bins are empty; at this n, 10 bins is
    already generous — read alongside the table's per-bin counts.

    USED BY: forward-validation-adjacent calibration reads over the
    calibration_table bins; the challenger's isotonic step exists precisely to
    drive this toward 0 on the validation window. Delegates to
    eval/np_metrics.expected_calibration_error."""
    return _np_expected_calibration_error(y_true, p, bins=bins)


def spearman_rho(a, b) -> float:
    """Spearman rank correlation:  ρ = corr(rank(a), rank(b)), average-rank ties.

    Pearson correlation of the rank transforms — measures MONOTONE agreement
    between two orderings while ignoring scale, which is the right comparison
    between the 0-100 hand score and a challenger probability (they share no
    scale, only an intended ordering). Invariant to any strictly increasing
    transform of either input; ties get the mean of their rank range. NaN with
    fewer than 2 pairs or a constant input (rank variance 0).

    USED BY: challenger/gbt_shadow divergence logging — ρ(score, challenger_p)
    plus the top-10 rank disagreements. Delegates to
    eval/np_metrics.spearman_rho."""
    return _np_spearman_rho(a, b)


def isotonic_calibrate(scores, outcomes, eval_scores=None) -> np.ndarray:
    """Isotonic (PAVA) probability calibration:
        f* = argmin_{f non-decreasing} Σ_i (y_i − f(s_i))²

    The least-squares projection of outcomes onto the cone of monotone
    functions of the score. Pool-adjacent-violators solves it exactly: scan
    scores ascending, pool any adjacent blocks whose means violate
    monotonicity; the solution is piecewise-constant at the pooled block means
    (each fitted value = the empirical positive rate over its block, so the
    output is a calibrated probability wherever y ∈ {0,1}). Nonparametric —
    assumes ONLY that true P(watch) is monotone in the score, which is the
    entire premise of ranking by it. Prediction interpolates linearly between
    block breakpoints and clamps flat at the ends (no extrapolated
    probabilities). Why not Platt scaling at this n: see MATH_FOUNDATION §5 —
    the hand score is a capped sum with no reason to be logit-linear, and
    Platt would impose that parametric shape on it.

    USED BY: challenger/gbt_shadow (fits on the temporal validation window,
    persists breakpoints to *.calib.json); ml_train_challenger. Delegates to
    eval/np_metrics.isotonic_fit + isotonic_predict; fit-then-predict at
    ``eval_scores`` (default: the training scores themselves). For persistable
    breakpoints use isotonic_fit/isotonic_predict directly (also re-exported
    from foundation)."""
    bx, by = isotonic_fit(scores, outcomes)
    if eval_scores is None:
        eval_scores = scores
    return isotonic_predict(bx, by, eval_scores)


# ── 4. survival / recency (Stage 5a) ──────────────────────────────────────────

def discrete_hazard(complete_gaps, censored_gaps,
                    bucket_days: int = DEFAULT_BUCKET_DAYS,
                    max_days: int = DEFAULT_MAX_DAYS) -> "tuple[np.ndarray, np.ndarray]":
    """Discrete-time hazard (life-table estimator):  ĥ_b = d_b / r_b.

    d_b = inter-watch gaps ENDING in day-bucket b (the next watch arrived
    then); r_b = gaps AT RISK at b's start — every gap, complete or censored,
    that survived to reach the bucket. This is the actuarial / Kaplan-Meier
    estimator on a bucket grid: conditioning on r_b is exactly what makes
    right-censoring ignorable (a censored gap contributes exposure to every
    bucket it survived, then exits without asserting an event — dropping
    censored gaps instead would bias hazards upward for long waits). Each ĥ_b
    is a binomial proportion given r_b: unbiased for the discrete hazard,
    variance ĥ_b(1−ĥ_b)/r_b — thin tails (small r_b) are visibly noisy, which
    the report prints at-risk counts to expose. Bucketing (default 7d) trades
    resolution for r_b per cell at n≈931 events.

    USED BY: likelihood/survival (fit_survival_model builds every curve with
    this), ml_survival_report. Delegates to survival.hazard_curve; returns
    (hazard, at_risk) arrays of length ceil(max_days/bucket_days)."""
    return _sv_hazard_curve(list(complete_gaps), list(censored_gaps),
                            bucket_days, max_days)


def residual_watch_probability(hazard, days_since_last: float, horizon: float,
                               bucket_days: int = DEFAULT_BUCKET_DAYS) -> float:
    """Residual watch probability (product-limit form):
        P(event in (g, g+H] | survived g)  =  1 − Π_b (1 − ĥ_b · frac_b)

    over the buckets b covered by the window (g, g+H]; frac_b ∈ (0,1] scales a
    partially-covered bucket's hazard linearly; beyond the fitted curve the
    LAST bucket's hazard extends flat. The complement of the conditional
    Kaplan-Meier survivor: having already survived g days costs nothing —
    conditioning is just starting the product at g instead of 0, which is the
    whole point of hazard (vs density) parameterization. Independence of
    bucket survivals is by construction of the discrete-time factorization,
    not an extra assumption. Clamped to [0,1].

    NOTE the sibling: survival.residual_watch_probability(entity_history, …)
    is the entity-history CONVENIENCE wrapper (gaps → curve → blend → this
    product); the canonical form here takes the hazard curve itself. Both
    delegate to the same survival.residual_probability_from_hazard, so they
    cannot diverge.

    USED BY: SurvivalModel.residual_watch_probability, ml_survival_report's
    residual table; the future grace/JIT derivations (MATH_FOUNDATION §6, §9)
    are expressed as thresholds on this quantity. Delegates to
    survival.residual_probability_from_hazard."""
    return _sv_residual_probability(np.asarray(hazard, dtype=float),
                                    days_since_last, horizon, bucket_days)


def empirical_bayes_pool(child, child_n: float, parent,
                         k: float = DEFAULT_K):
    """Empirical-Bayes shrinkage:  ĥ = w·ĥ_child + (1−w)·ĥ_parent,  w = n/(n+k).

    Beta-Binomial reading: give the bucket hazard a Beta prior centered on the
    parent pool with prior strength k pseudo-events, Beta(k·h̄, k·(1−h̄)); after
    observing d events in n at-risk the posterior mean is
        (d + k·h̄) / (n + k)  =  w·(d/n) + (1−w)·h̄,   w = n/(n+k)
    — exactly this blend. k = 5 (DEFAULT_K) says "a title's own history starts
    outweighing the pool at about 5 events": n=0 → pure parent, n=k → 50/50,
    n≫k → the title speaks for itself. The production hierarchy applies it
    twice — title (≥3 events) → franchise/collection pool → household — with
    ONE w per curve from the entity's EVENT count (a deliberate small-n
    simplification vs per-bucket at-risk weighting, documented in
    MATH_FOUNDATION §6). Shrinkage trades a little bias toward the pool for a
    large variance reduction on sparse titles — the classical small-n move.

    USED BY: survival.SurvivalModel.curve_for (the two-level blend),
    survival.residual_watch_probability's pooled path, ml_survival_report.
    Delegates to survival.blend_hazards (child None or child_n ≤ 0 → parent
    copy). Arrays in, array out; scalars ride through as 0-d arrays."""
    parent_arr = np.asarray(parent, dtype=float)
    child_arr = None if child is None else np.asarray(child, dtype=float)
    return _sv_blend_hazards(child_arr, float(child_n), parent_arr, k=float(k))


# ── 5. space decision theory (Stage 5b + coordinator) ─────────────────────────

def value_density(score_or_p, gb) -> float:
    """Value density:  ρ = v / max(GB, 0.1).

    The greedy key for the min-value covering knapsack: to reclaim ``need``
    GB while destroying the least retained value Σv, sort ascending by v/GB
    and take from the bottom. In the LP relaxation (allow fractional
    deletion), density order is EXACTLY optimal — the classic
    Dantzig/exchange argument: swapping any selected item for an unselected
    one with lower density weakly increases value destroyed per GB reclaimed
    — and the integral greedy solution differs from the LP optimum by at most
    the one boundary item, negligible when single files are small next to the
    multi-hundred-GB target. The 0.1 GB floor keeps near-zero-size files from
    dividing by ~0 and jumping the queue (a 1 MB file is not "free value").
    v is today the watchability score; under a calibrated model it becomes
    p̂·v (expected utility per GB — MATH_FOUNDATION §7).

    USED BY: space/coordinator_ranker.select_for_target ranking_mode
    "utility_per_gb" (config ``space_delete_ranking``, default-off Stage 5b);
    the default "score" mode ranks by score alone and is untouched. Delegates
    to coordinator_ranker.utility_per_gb — same clamp, same falsy-GB
    handling, so this can never disagree with the ranker's key."""
    return _ranker_utility_per_gb({"size_gb": gb}, float(score_or_p))


def downgrade_credit(need: float, projected_reclaim, ratio: float) -> float:
    """Downgrade-first deletion target:
        delete_target = max(0,  need  −  clamp(ratio, 0, 1) · Σ_i max(reclaim_i, 0))

    Downgrades reclaim space non-destructively but LATER (profile flip now,
    smaller file when the re-grab imports). Deleting to cover the full
    deficit while re-grabs are in flight double-counts: titles die that the
    downgrades would have paid for. So the ledger's projected downgrade
    reclaim (positive stamps only — a negative/NaN projection is not reclaim)
    is credited against the deficit, scaled by a confidence ratio (1.0 = full
    credit, 0.0 = legacy delete-covers-everything), and deletions cover only
    the remainder. Equivalent to the coordinator's two-step
    credit = min(need, Σ·r); need −= credit — the min() and the outer max(0,·)
    are the same clamp. Idempotent per run: each run recomputes from actual
    free space, so realized downgrades shrink ``need`` itself.

    USED BY (pure mirror — the coordinator is NOT rewired to call this):
    services/coordinator/space_coordinator, need = max(0, U − free), reclaim
    from _projected_downgrade_gb (ledger rows planned_action='downgrade' with
    plan_reclaim_gb > 0), ratio from config ``space_downgrade_credit_ratio``.
    test_foundation asserts equivalence against that arithmetic.

    ``projected_reclaim``: iterable of per-title projected GB (or one scalar);
    non-finite entries are skipped, matching the coordinator's
    pd.to_numeric(errors="coerce") + ``> 0`` filter."""
    r = min(max(float(ratio), 0.0), 1.0)
    if np.isscalar(projected_reclaim) or isinstance(projected_reclaim, (int, float)):
        projected_reclaim = [projected_reclaim]
    total = 0.0
    for v in projected_reclaim:
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if np.isfinite(fv) and fv > 0:
            total += fv
    return max(0.0, float(need) - r * total)
