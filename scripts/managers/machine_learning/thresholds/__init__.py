"""
thresholds/ — calibrated-probability threshold derivation (MATH_FOUNDATION §9).
================================================================================
Every decision cutoff in Glidearr is a hand-set point on the 0-100 watchability
scale: monitor an owned movie at ``>= 30``, keep a series monitored at ``>= 35``,
let the coordinator delete below ``20``, route to 4K at ``>= 70``. Those numbers
mean nothing outside this household and nothing after a scorer change — the same
"35" is a different decision the moment a signal group is reweighted.

This package replaces the ANCHOR (not yet the behaviour): a threshold becomes
*"act when P(watch within H) >= p"*, and the score cutoff that implements it is
DERIVED by inverting the isotonic score→probability map (§5) re-fit from the
household's own matured labels every run. A probability target survives scorer
changes; the score cutoff underneath it moves so the decision does not.

Rollout is SHADOW-FIRST and the default is byte-identical to today:

  * ``derive``   — fit the calibrator, invert it, and SHRINK the result toward
                   the hand-set constant by ``w = n_pos/(n_pos+k)`` (the
                   survival model's own empirical-Bayes pool). Evidence is a
                   dial, not a switch: at n_pos=0 the effective value is the
                   constant bit-identically, at n_pos=k the two weigh equally.
                   The only hard refusal left is "no evidence at all".
  * ``shadow``   — pure comparison: current constant vs raw derived vs effective
                   (shrunk) equivalent, and exactly how many entities would flip
                   sides at the effective cutoff.
  * ``registry`` — the single accessor consumers read through
                   (:func:`registry.get_threshold`). In the DEFAULT mode
                   ``"shadow"`` it returns the caller's own literal, unchanged.
  * ``report``   — the end-of-run table + the audit JSON
                   (``<cache>/ml/reports/thresholds_{date}.json``).

Config (all under ``ml.thresholds``)::

    ml.thresholds.mode              "shadow" (DEFAULT) | "derived" | "off"
    ml.thresholds.horizon_days      14
    ml.thresholds.targets.acquire / .monitor / .delete / .uhd
    ml.thresholds.shrinkage_k       150 — n_pos at which derived == constant 50/50
    ml.thresholds.include_backfill  TRUE — a fresh install's only labels are the
                                    reconstructed ones (see registry)
    ml.thresholds.max_fit_days      365 (0 = the whole snapshot history)

Nothing here can change a decision until ``mode`` is set to ``"derived"``, and
even then the value a consumer receives has been pulled back toward its own
literal in proportion to the evidence — with no evidence at all it IS the
literal, and one loud warning says so.

IMPORT COST: this ``__init__`` is deliberately EMPTY at import time (PEP 562
lazy attributes). Every routed consumer does
``from ...thresholds.registry import get_threshold`` on a hot service path, and
importing that submodule runs this file first — so pulling numpy/pandas/
foundation in here would tax the whole run for a feature that is off by default.
``from ...thresholds import fit_calibrator`` still works; it just resolves on
first use.
"""
from importlib import import_module

_LAZY = {
    "CalibrationResult": "derive", "fit_calibrator": "derive",
    "derive_threshold": "derive", "data_gate": "derive",
    "mature_candidates": "derive", "MIN_POS_FOR_DERIVATION": "derive",
    "MIN_POS_FOR_FIT": "derive", "DEFAULT_SHRINKAGE_K": "derive",
    "blend_threshold": "derive", "shrinkage_weight": "derive",
    "confidence_label": "derive",
    "compare": "shadow", "render_rows": "shadow",
    "ThresholdSpec": "registry", "THRESHOLD_SPECS": "registry",
    "DEFAULT_TARGET_P": "registry", "get_threshold": "registry",
    "threshold_mode": "registry", "target_probabilities": "registry",
    "MODE_SHADOW": "registry", "MODE_DERIVED": "registry", "MODE_OFF": "registry",
}

__all__ = sorted(_LAZY)


def __getattr__(name):
    mod = _LAZY.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(f"{__name__}.{mod}"), name)


def __dir__():
    return sorted(set(list(globals()) + __all__))
