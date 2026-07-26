"""
foundation/ — the canonical mathematical foundation of the ML layer.
================================================================================
CONTRACT: every statistical formula used by Glidearr is defined here or
re-exported here; adopters import from foundation, never reimplement. A formula
that exists twice will drift twice — this package is the single import surface
over the operative implementations (eval/np_metrics, likelihood/survival,
space/coordinator_ranker), wrapping them thinly where a canonical signature or
a rigorous docstring is worth adding, re-exporting them directly where they
already ARE the canonical form. Nothing is duplicated: every wrapper delegates,
so foundation can never disagree with what the tools and planners compute.

Formal reference: ../MATH_FOUNDATION.md (notation, derivations, estimator
properties, the adoption roadmap from hand-set constants to derived
quantities). Each docstring in formulas.py is the executable index into it.

NOT wired into the runtime: no module under scripts/managers imports
foundation on the run path; the production scorers (scoring/movie_scorer,
scoring/show_scorer) and the coordinator keep their own operative code —
linear_utility / value_density / downgrade_credit document and mirror them,
proven equivalent by test_foundation.py. PURE throughout — no I/O, no HTTP,
no global_cache, no clock.

Model containers stay home: likelihood/survival.SurvivalModel /
fit_survival_model (model assembly, not formulas) and the scorers' group
logic are referenced by the docs, not re-exported.
"""
from scripts.managers.machine_learning.eval.np_metrics import (
    calibration_table,
    isotonic_fit,
    isotonic_predict,
    minmax_scale,
)
from scripts.managers.machine_learning.foundation.formulas import (
    average_precision,
    brier_score,
    discrete_hazard,
    downgrade_credit,
    empirical_bayes_pool,
    expected_calibration_error,
    fit_logistic_irls,
    isotonic_calibrate,
    linear_utility,
    logistic_probability,
    residual_watch_probability,
    spearman_rho,
    standardize,
    suggested_weight_multipliers,
    value_density,
)
from scripts.managers.machine_learning.likelihood.survival import entity_gaps

__all__ = [
    # production model form
    "linear_utility",
    # logistic refit
    "standardize", "logistic_probability", "fit_logistic_irls",
    "suggested_weight_multipliers",
    # evaluation
    "average_precision", "brier_score", "expected_calibration_error",
    "calibration_table", "minmax_scale", "spearman_rho",
    # calibration
    "isotonic_calibrate", "isotonic_fit", "isotonic_predict",
    # survival
    "discrete_hazard", "residual_watch_probability", "empirical_bayes_pool",
    "entity_gaps",
    # space decision theory
    "value_density", "downgrade_credit",
]
