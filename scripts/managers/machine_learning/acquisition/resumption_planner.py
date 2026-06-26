"""Series & Saga Resumption — the PURE prioritization math (the release-proximity ramp + the
affinity/proximity blend). See ``machine_learning/DESIGN_series_saga_resumption.md`` §4.

Resumption circles the rolling window BACK: when you're about to return to a show, or the world
dates a new season / a sequel from cast & crew you love, it re-acquires the recent prior content
and RAMPS priority as the release nears so you're caught up before you sit down. This module is
the net-new math the design calls out (the codebase had no countdown/ramp logic before this); the
orchestration (the three triggers, candidate enumeration) and all I/O live in the service-side
``ResumptionManager``. PURE — ``d`` / ``today`` are injected, no clock, no I/O (brain-purity
guarded under the already-listed ``acquisition`` subpackage)."""
from __future__ import annotations

import math


def ramp(d, *, window_days: float = 60, ready_by_days: float = 7,
         grace_window_days: float = 14, decay_tau_days: float = 30) -> float:
    """The release-proximity ramp ``R(d) ∈ [0, 100]`` — "how close is the moment?". ``d`` is the
    days until the triggering release (NEGATIVE once it has released). Piecewise (design §4.1):

        d > W            → 0                          (too far out — ignore for now)
        R0 < d ≤ W       → 100·(W − d)/(W − R0)       (approaching — linear rise 0→100)
        −G ≤ d ≤ R0      → 100                        (ready window through grace — max)
        d < −G           → 100·exp(−(−d − G)/τ)       (released a while ago — exp decay)

    It peaks at ``d = R0`` (``ready_by_days`` — be caught up a little EARLY so the re-grab can
    finish), holds max through ``G`` days after release (you can still catch up if it just
    dropped), then decays with constant ``τ`` (half-life ≈ ``τ·ln2``). A non-numeric / undated
    ``d`` → 0, so undated content is never floated to the top. Boundaries are continuous."""
    try:
        d = float(d)
    except (TypeError, ValueError):
        return 0.0
    w = float(window_days)
    r0 = float(ready_by_days)
    g = max(0.0, float(grace_window_days))
    tau = float(decay_tau_days)
    if d > w:
        return 0.0
    if d > r0:                                   # R0 < d ≤ W — approaching, linear rise
        span = w - r0
        return 100.0 * (w - d) / span if span > 0 else 100.0
    if d >= -g:                                  # −G ≤ d ≤ R0 — ready window + grace, hold max
        return 100.0
    if tau <= 0:                                 # past grace with no decay constant → fall to 0
        return 0.0
    return 100.0 * math.exp(-(-d - g) / tau)     # d < −G — exponential decay


def priority(s_prior, r, *, weight_affinity: float = 0.5, weight_proximity: float = 0.5) -> float:
    """The resumption priority ``P = clamp(w_s·S_prior + w_r·R, 0, 100)`` (design §4) — blends HOW
    MUCH YOU CARE (``s_prior``, the 0–100 watchability/affinity of the content being re-acquired)
    with HOW CLOSE THE MOMENT IS (``r``, the ramp ``R(d)``). Weights default 0.5/0.5. Non-numeric
    inputs coerce to 0; the result is clamped to [0, 100] (so weights summing > 1 can't overflow)."""
    return max(0.0, min(100.0, _num(weight_affinity) * _num(s_prior)
                        + _num(weight_proximity) * _num(r)))


def resumption_priority(s_prior, days_to_release, *, config=None) -> float:
    """End-to-end ``priority(s_prior, ramp(days_to_release, …))`` reading the ramp window + blend
    weights from a ``config`` dict (the ``acquisition.resumption`` block) with the design defaults.
    ``days_to_release is None`` is a TRIGGER-1 "return now" → ``d := ready_by_days`` so the ramp
    sits at max (returning is "now", design §4.1). PURE — pass the precomputed day delta in."""
    cfg = config or {}
    r0 = cfg.get("ready_by_days", 7)
    d = r0 if days_to_release is None else days_to_release       # trigger-1 resume → ready window
    r = ramp(d, window_days=cfg.get("ramp_window_days", 60), ready_by_days=r0,
             grace_window_days=cfg.get("grace_window_days", 14),
             decay_tau_days=cfg.get("decay_tau_days", 30))
    return priority(s_prior, r, weight_affinity=cfg.get("weight_affinity", 0.5),
                    weight_proximity=cfg.get("weight_proximity", 0.5))


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0
