"""
thresholds/derive.py — score cutoffs derived from calibrated probability (§5 + §9).
================================================================================
Turns *"acquire when the score is at least 35"* into *"acquire when
P(watch within H) >= p"*, and computes the score cutoff that implements it by
inverting the isotonic score→probability map fit on the household's own matured
labels. The probability target is portable (it means the same thing in every
household and after every scorer change); the score cutoff underneath it is a
derived, disposable implementation detail that the isotonic re-fit moves.

Four functions, all pure except for the label join the caller hands in:

    fit_calibrator(snapshots_df, horizon_days)  -> CalibrationResult | None
    derive_threshold(calibrator, target_p)      -> float   (score in [0, 100])
    blend_threshold(derived, constant, n_pos)   -> (effective, w)
    data_gate(n_pos)                            -> (ok, reason)   [annotation]

MATURITY + TEMPORAL SAFETY (the same rule ``ml_forward_validation`` uses)
------------------------------------------------------------------------
Only rows whose horizon has fully elapsed carry a trustworthy negative, so the
fit uses ``label_mature`` rows exclusively (``labels/labeling.build_labels``
stamps it: ``snapshot_ts + H <= now``). That single rule also makes the fit
temporally safe by construction for the runtime use: every fit row closed its
horizon strictly before ``now``, while the entities the report then classifies
are the library's scores *today* — the fit window and the reported rows cannot
overlap. When an explicit backtest split is wanted, ``fit_before=<YYYY-MM-DD>``
truncates the fit window further and the caller reports on rows at/after it.

PROVENANCE — and why THRESHOLDS opt IN to backfill
--------------------------------------------------
This function keeps ``include_backfill=False`` (a pure estimator should not
quietly widen its own evidence base), but the threshold CONFIG that drives it —
``ml.thresholds.include_backfill`` — defaults **TRUE**, the one place in the ML
stack that does. A fresh install's only labels are reconstructed ones: the
prospective pipeline starts logging on installation day and produces its first
matured label a horizon later, so excluding backfill means excluding everything.
The three known leakage channels (``credits_today, metadata_today,
deletions_unknown``) bias a *claim about accuracy* — an AP/AUC number — far more
than they bias a *monotone score→P map*: they perturb where individual titles sit
on the score axis, while the calibrator only needs the ordering to be roughly
right to place a cutoff. The rows are still tagged, the report always splits
``n_pos`` by source, and forward validation (which IS an accuracy claim) keeps
excluding them. See ``report.build_report``.

FROM A GATE TO A DIAL — EMPIRICAL-BAYES SHRINKAGE (§10)
-------------------------------------------------------
The first cut of this module refused outright below ``MIN_POS_FOR_DERIVATION =
300`` positives, so a real install read "0/16 cleared the data gate …
insufficient data (n_pos=0, need 300)" on every row and stayed there for weeks.
A binary gate is also the wrong shape for the statistics underneath it: evidence
does not arrive in one step at 300, it accumulates. So the hand-set constant and
the derived value are POOLED, using exactly the estimator the survival model
already applies to sparse titles (``foundation.empirical_bayes_pool``, §6) —
there is deliberately no second blend implementation in this package::

    effective = w * derived + (1 - w) * constant,      w = n_pos / (n_pos + k)

Beta-Binomial reading: the constant is a prior worth ``k`` pseudo-positives, the
household's matured labels are the data. ``k = 150``
(``ml.thresholds.shrinkage_k``) puts the 50/50 crossover midway between §10's
100 rung (the power floor where a large effect becomes merely *detectable*) and
its 300 rung (where calibration curves and threshold derivation settle) — the
derived value is trusted half-way exactly where §10 says it stops being a rumour
and has not yet become stable. In numbers::

    n_pos     0     38    100    150    300    1000
    w       0.00   0.20   0.40   0.50   0.67   0.87

``n_pos = 0`` returns the parent untouched — ``effective`` is the constant
BIT-IDENTICALLY, so a cold install behaves exactly as it did before this package
existed. Shrinkage is for "some evidence", never for "no evidence": below
``MIN_POS_FOR_FIT = 5`` positives — or with no labels, a constant score, or a
degenerate/non-monotone map — ``fit_calibrator`` returns ``None``, there is no
``derived`` term at all, and the effective value is the constant with the reason
stated. That hard floor is what remains of the gate.

§10's ladder survives as a CONFIDENCE ANNOTATION printed next to each row —
``directional`` (<100), ``usable`` (>=100), ``stable`` (>=300),
``magnitude-grade`` (>=1000). It no longer decides the value; it describes how
far the value was allowed to move, which is what the measured ±0.2-class
magnitude noise floor (§11) actually justifies. :func:`data_gate` is kept for
that annotation and for the opt-in ``require_gate=True`` diagnostic — never as a
veto.

DEFAULT TARGET PROBABILITIES — how they were chosen (the inversion)
-------------------------------------------------------------------
The defaults in ``registry.DEFAULT_TARGET_P`` are not opinions; they are today's
constants pushed through today's calibrator, so that a household flipping
``ml.thresholds.mode`` to ``"derived"`` gets as close to its current behaviour as
the calibrator can express. Measured on this repo's real snapshot store
(``scripts/support/cache``, H=14d, radarr, ALL matured rows — which today are
exclusively ``source="backfill"`` truncated-replay rows, n=11443, n_pos=38,
base rate 0.0033)::

    score  20  ->  P 0.002322     (delete band floor)
    score  30  ->  P 0.029851     (owned re-monitor)
    score  35  ->  P 0.037815     (series monitor / acquire)
    score  70  ->  P 0.037815     (4K routing — SATURATED, see below)

Reproduce (ECE 9.6e-05, 53 knots, fit window 2026-06-05 … 2026-07-10)::

    PYTHONPATH=. python3 -c "\
    from scripts.managers.machine_learning.labels.snapshots import load_snapshots; \
    from scripts.managers.machine_learning.thresholds import fit_calibrator; \
    c = fit_calibrator(load_snapshots('scripts/support/cache'), 14, \
                       history='scripts/support/cache', service='radarr', \
                       include_backfill=True, require_gate=False); \
    print([(s, round(c.p_at(s), 6)) for s in (20, 30, 35, 70)])"

Hence ``{acquire: 0.0378, monitor: 0.0298, delete: 0.0023, uhd: 0.0378}`` — each
value TRUNCATED, never rounded up, to four significant figures. Rounding 0.037815
*up* to 0.038 would name a probability no score on the calibrator reaches, and
the inverse would clamp to 100 ("nothing qualifies") — the worst possible
reproduction of a cutoff of 35. Truncating lands the inverse at 31.0 instead.

Three honest caveats, all of which the shadow table exists to surface:

* **The map saturates at score 31.** The household's highest *matured* score is
  56 and only 38 watches exist, so isotonic pools everything above 31 into one
  block — score 35 and score 70 have the *same* estimated probability. The uhd
  target is therefore identical to the acquire target today; it separates only
  when higher-scoring titles accumulate labels. This is a data limit, not a
  modelling choice, and it is precisely why the §10 gate refuses.
* **Plateaus make the inverse coarse.** The calibrator has 7 distinct fitted
  values over the whole 0-100 range. Inverting P(20)=0.00232 returns 12 — the
  *smallest* score with that probability — because the calibrator genuinely
  cannot tell 12 from 20. The derived cutoff is decision-equivalent to the
  constant only up to that resolution; the gap is reported as a flip count, not
  hidden. See :func:`derive_threshold` for why the lower inverse is the only
  semantically correct choice for a ``>=`` rule.
* **A target must be truncated, not rounded.** Because the map clamps flat at
  its top (§5 — no extrapolated probabilities), a target even slightly above
  the highest fitted value inverts to 100.0, i.e. "no score qualifies". Any
  operator-supplied target inherits that behaviour, deliberately: asking for
  odds the household's history has never produced must fail loudly (nothing
  passes), not quietly (everything passes).

Those numbers were computed from BACKFILL rows because no prospective row has
matured yet (§11) — ``directional``, the lowest confidence rung. They are
defaults for a mode that is off by default; and even in ``mode="derived"`` the
shrinkage weight at n_pos=38 is 0.20, so they can move a cutoff by a fifth of the
gap, not replace it.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.managers.machine_learning.foundation import (
    empirical_bayes_pool,
    expected_calibration_error,
    isotonic_fit,
    isotonic_predict,
)

# ── §10 milestone ladder ──────────────────────────────────────────────────────
GATE_MILESTONES = (100, 300, 1000)
MIN_POS_FOR_DERIVATION = 300     # §10: threshold derivation stabilises here
DEFAULT_HORIZON_DAYS = 14        # matches ml_forward_validation's default

# ── shrinkage (the gate, as a dial) ───────────────────────────────────────────
#: Prior strength in ``w = n_pos / (n_pos + k)``: the number of matured POSITIVE
#: labels at which the derived cutoff and the hand-set constant weigh equally.
#: 150 sits midway between §10's 100 ("usable" — a large effect is detectable)
#: and 300 ("stable" — calibration curves and threshold derivation settle).
DEFAULT_SHRINKAGE_K = 150.0

#: Hard floor: fewer positives than this and there is no ``derived`` term at all
#: (shrinkage is for "some evidence", not "no evidence"). A handful of positives
#: cannot even produce two distinct isotonic blocks worth inverting.
MIN_POS_FOR_FIT = 5

#: §10's ladder, demoted from gate to annotation. ``(floor, label)`` descending.
CONFIDENCE_NONE = "none"
CONFIDENCE_TIERS = ((GATE_MILESTONES[2], "magnitude-grade"),   # >= 1000
                    (GATE_MILESTONES[1], "stable"),            # >= 300
                    (GATE_MILESTONES[0], "usable"),            # >= 100
                    (1, "directional"))                        # >= 1

SCORE_MIN = 0.0
SCORE_MAX = 100.0

_LABEL_COLS = ("watched_within_h", "label_mature")


# ── the fitted map ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CalibrationResult:
    """An isotonic score→P(watch within H) map plus everything needed to judge it.

    ``knots_score`` / ``knots_p`` are the DEDUPLICATED PAVA breakpoints (one
    entry per distinct score, carrying that score's largest fitted value), so
    :meth:`predict` and :func:`derive_threshold` are exact inverses of each
    other — a property the tests assert. Storing unique scores also keeps the
    persisted JSON small (53 knots instead of 11443 rows on the real store).

    ``sources`` counts rows by provenance BEFORE the maturity cut, so a report
    can say "4166 prospective rows exist, none matured yet" rather than just
    reporting zero. ``n_pos_by_source`` counts the POSITIVES that actually
    reached the fit, by provenance — the evidence's provenance is never hidden
    behind one aggregate number.
    """

    knots_score: tuple = ()
    knots_p: tuple = ()
    horizon_days: int = DEFAULT_HORIZON_DAYS
    n: int = 0
    n_pos: int = 0
    base_rate: float = 0.0
    service: "str | None" = None
    gate_ok: bool = False
    gate_reason: str = ""
    fit_from: "str | None" = None      # earliest snapshot_date in the fit window
    fit_to: "str | None" = None        # latest snapshot_date in the fit window
    fit_before: "str | None" = None    # explicit backtest split, when given
    include_backfill: bool = False
    ece: "float | None" = None
    sources: dict = field(default_factory=dict)
    n_pos_by_source: dict = field(default_factory=dict)

    # -- use --------------------------------------------------------------
    def predict(self, scores) -> np.ndarray:
        """Calibrated P(watch within H) for *scores* (clamped flat at the ends —
        no extrapolated probabilities, §5)."""
        return isotonic_predict(np.asarray(self.knots_score, dtype=float),
                                np.asarray(self.knots_p, dtype=float), scores)

    def p_at(self, score) -> float:
        """Scalar convenience wrapper over :meth:`predict`."""
        return float(self.predict([float(score)])[0])

    @property
    def p_min(self) -> float:
        return float(self.knots_p[0]) if self.knots_p else float("nan")

    @property
    def p_max(self) -> float:
        return float(self.knots_p[-1]) if self.knots_p else float("nan")

    def to_dict(self, *, with_knots: bool = True) -> dict:
        """JSON-safe summary for ``<cache>/ml/reports/thresholds_{date}.json``."""
        out = {
            "service": self.service,
            "horizon_days": self.horizon_days,
            "n": self.n,
            "n_pos": self.n_pos,
            "base_rate": round(float(self.base_rate), 6),
            "gate_ok": bool(self.gate_ok),
            "gate_reason": self.gate_reason,
            "fit_from": self.fit_from,
            "fit_to": self.fit_to,
            "fit_before": self.fit_before,
            "include_backfill": bool(self.include_backfill),
            "ece": (round(float(self.ece), 6) if self.ece is not None
                    and np.isfinite(self.ece) else None),
            "p_min": round(self.p_min, 6) if self.knots_p else None,
            "p_max": round(self.p_max, 6) if self.knots_p else None,
            "n_knots": len(self.knots_score),
            "sources": dict(self.sources or {}),
            "n_pos_by_source": dict(self.n_pos_by_source or {}),
            "confidence": confidence_label(self.n_pos),
        }
        if with_knots:
            out["knots_score"] = [round(float(v), 4) for v in self.knots_score]
            out["knots_p"] = [round(float(v), 6) for v in self.knots_p]
        return out


# ── shrinkage: the constant is the prior, the labels are the data ─────────────

def _as_n(n_pos) -> int:
    try:
        n = int(n_pos)
    except (TypeError, ValueError):
        return 0
    return max(0, n)


def shrinkage_weight(n_pos, k: float = DEFAULT_SHRINKAGE_K) -> float:
    """``w = n_pos / (n_pos + k)`` — how much of the DERIVED value survives.

    The identical expression ``foundation.empirical_bayes_pool`` uses internally
    (via ``survival.blend_hazards``), computed here only so the report can PRINT
    the weight; :func:`blend_threshold` never re-implements the blend itself.

    Monotone non-decreasing in ``n_pos``, 0 at ``n_pos=0``, exactly 0.5 at
    ``n_pos == k``, → 1 as ``n_pos → ∞``. A non-positive ``k`` means "no prior"
    (w = 1 for any evidence at all); ``n_pos = 0`` always wins and returns 0.0,
    so ``0/(0+0)`` never becomes a NaN."""
    n = _as_n(n_pos)
    if n <= 0:
        return 0.0
    kf = float(k)
    if not np.isfinite(kf) or kf <= 0.0:
        return 1.0
    return float(n) / (float(n) + kf)


def blend_threshold(derived, constant, n_pos,
                    k: float = DEFAULT_SHRINKAGE_K) -> "tuple[float, float]":
    """``(effective, w)`` — the cutoff a consumer in ``mode="derived"`` gets.

        effective = w * derived + (1 - w) * constant,   w = n_pos / (n_pos + k)

    Delegates the arithmetic to :func:`foundation.empirical_bayes_pool` (the
    survival model's shrinkage, MATH_FOUNDATION §6) with the hand-set constant as
    the PARENT and the derived cutoff as the CHILD, so the two blends in this
    codebase can never drift apart.

    ``derived is None`` (no calibrator: no labels, too few positives, degenerate
    map) → the helper's ``child is None`` branch returns the parent object
    itself, i.e. ``effective == float(constant)`` **exactly**, w = 0.0. The same
    is true at ``n_pos <= 0``. That bit-identity is what makes a cold install
    behave precisely as it does today, and it is asserted in the tests."""
    c = float(constant)
    n = _as_n(n_pos)
    child = None if derived is None else float(derived)
    if child is not None and not np.isfinite(child):
        child = None
    eff = float(empirical_bayes_pool(child, float(n), c, k=float(k)))
    return eff, (shrinkage_weight(n, k) if child is not None else 0.0)


def confidence_label(n_pos, has_derived: bool = True) -> str:
    """§10's ladder as an ANNOTATION: how much evidence stands behind the move.

    ``none`` (no derived value at all, or zero positives) → ``directional``
    (<100: order suggestive, magnitudes noise) → ``usable`` (>=100: the §4 power
    floor, large effects detectable) → ``stable`` (>=300: calibration curves and
    threshold derivation settle) → ``magnitude-grade`` (>=1000: the top rung).

    It gates NOTHING — the shrinkage weight already scales the value. It tells
    the reader how seriously to take the distance between ``current`` and
    ``effective``."""
    n = _as_n(n_pos)
    if not has_derived or n <= 0:
        return CONFIDENCE_NONE
    for floor, label in CONFIDENCE_TIERS:
        if n >= floor:
            return label
    return CONFIDENCE_NONE


# ── §10 data gate (annotation + opt-in diagnostic) ────────────────────────────

def data_gate(n_pos) -> "tuple[bool, str]":
    """Is a derived threshold trustworthy at this many positives? (§10)

    NO LONGER A VETO. The value is now shrunk toward the constant by
    :func:`blend_threshold`, so this reports whether the §10 "stable" milestone
    was reached — the ``gate_ok`` annotation in the audit JSON, the wording the
    report reuses, and the opt-in ``fit_calibrator(require_gate=True)``
    diagnostic. The hard floor that DOES refuse is :data:`MIN_POS_FOR_FIT`.

    The milestone ladder is 100 / 300 / 1000 matured POSITIVE labels — it comes
    from the two-sided power identity ``n_pos ≈ 7.85/δ²`` (§11): ≈87 to merely
    *detect* a large (δ=0.3) effect, ≈350 at δ=0.15, ≈780 at δ=0.10. Threshold
    derivation needs the middle rung: below ~300 the isotonic map is a handful
    of pooled blocks whose breakpoints move by tens of score points between
    runs, so the "derived" cutoff would be noise wearing a probability's
    clothes.

    Returns ``(ok, reason)``; *reason* is written for a log line and always
    states the shortfall in the "insufficient data (n_pos=X, need Y)" form the
    shadow report prints verbatim.
    """
    try:
        n = int(n_pos)
    except (TypeError, ValueError):
        n = 0
    need = MIN_POS_FOR_DERIVATION
    _shrunk = (" — NOT refused: the derived cutoff is shrunk toward the hand-set "
               "constant at w=n_pos/(n_pos+k) (ml.thresholds.shrinkage_k).")
    if n < GATE_MILESTONES[0]:
        return False, (
            f"insufficient data (n_pos={n}, need {need}): below the §10 n_pos>="
            f"{GATE_MILESTONES[0]} power floor — at this count even the ORDER of "
            "the calibration blocks is unstable, let alone their breakpoints"
            + _shrunk)
    if n < need:
        return False, (
            f"insufficient data (n_pos={n}, need {need}): past the "
            f"{GATE_MILESTONES[0]}-positive power floor (weight-refit "
            "suggestions become readable) but below the §10 milestone at which "
            "calibration curves and threshold derivation stabilise" + _shrunk)
    if n < GATE_MILESTONES[2]:
        return True, (
            f"n_pos={n} >= {need} — §10 milestone for stable calibration curves "
            "and threshold derivation; magnitudes still carry the measured "
            "±0.2-class noise floor (§11), so treat a derived cutoff as good to "
            "a few score points, not to the point.")
    return True, (
        f"n_pos={n} >= {GATE_MILESTONES[2]} — §10's top rung; the calibration "
        "curve is the best-supported estimator in the stack at this n.")


# ── fit ───────────────────────────────────────────────────────────────────────

def _dedupe_knots(bx: np.ndarray, by: np.ndarray) -> "tuple[np.ndarray, np.ndarray]":
    """Collapse PAVA's per-observation output to one knot per distinct score.

    ``isotonic_fit`` returns one (x, fitted) pair per input row, so a score seen
    500 times appears 500 times — and, because PAVA pools by position in the
    sorted order rather than by tied x, a tie CAN straddle a block boundary and
    receive two different fitted values. Taking the LAST (largest, since fitted
    is non-decreasing in that order) value per distinct score yields a genuine
    non-decreasing step function of the score, which is what a threshold is
    inverted from."""
    if len(bx) == 0:
        return np.asarray([], dtype=float), np.asarray([], dtype=float)
    order = np.lexsort((by, bx))         # ascending x, then ascending fitted
    sx, sy = bx[order], by[order]
    keep = np.ones(len(sx), dtype=bool)
    keep[:-1] = sx[:-1] != sx[1:]        # keep the LAST row of each tie run
    kx, ky = sx[keep], sy[keep]
    ky = np.maximum.accumulate(ky)       # belt-and-braces monotonicity
    return kx.astype(float), ky.astype(float)


def mature_candidates(df, horizon_days: int, now=None):
    """Rows whose horizon has ALREADY closed — the only ones ``build_labels``
    can stamp ``label_mature`` on.

    ``build_labels`` walks rows in Python, so pre-filtering on ``snapshot_ts``
    (a vectorised comparison) before calling it is the difference between
    labelling the whole store and labelling the part that can matter. Pure
    optimisation: the surviving set is identical to filtering afterwards."""
    if df is None or getattr(df, "empty", True) or "snapshot_ts" not in df.columns:
        return df
    cutoff = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    if cutoff.tzinfo is None:
        cutoff = cutoff.tz_localize("UTC")
    cutoff = cutoff - pd.Timedelta(days=float(horizon_days))
    ts = pd.to_datetime(df["snapshot_ts"], utc=True, errors="coerce")
    return df[ts.notna() & (ts <= cutoff)]


def _prepare_rows(snapshots_df, horizon_days, *, history, now, service,
                  include_backfill, fit_before):
    """Maturity + provenance + service filtering → (scores, outcomes, meta)."""
    if snapshots_df is None or getattr(snapshots_df, "empty", True):
        return None
    df = snapshots_df
    if "watchability_score" not in df.columns:
        return None
    if service and "service" in df.columns:
        df = df[df["service"] == service]
    if df.empty:
        return None

    # provenance: backfilled rows carry KNOWN leakage — opt-in only.
    if "source" in df.columns:
        src = df["source"].fillna("prospective").replace("", "prospective")
        sources = {str(k): int(v) for k, v in src.value_counts(dropna=False).items()}
        if not include_backfill:
            df = df[src != "backfill"]
    else:
        sources = {"prospective": int(len(df))}
    if df.empty:
        return None

    # labels: reuse a pre-labeled frame, else join Tautulli ground truth.
    if not all(c in df.columns for c in _LABEL_COLS):
        if history is None:
            return None
        df = mature_candidates(df, horizon_days, now=now)
        if df is None or df.empty:
            return None
        from scripts.managers.machine_learning.labels.labeling import build_labels
        df = build_labels(df, history, horizon_days=horizon_days, now=now)
        if service and "service" in df.columns:
            df = df[df["service"] == service]

    # MATURITY — the only rows whose negatives are real (ml_forward_validation).
    df = df[df["label_mature"].astype("boolean").fillna(False).to_numpy(dtype=bool)]
    if df.empty:
        return None

    # explicit backtest split: fit strictly BEFORE the reported window.
    if fit_before and "snapshot_date" in df.columns:
        df = df[df["snapshot_date"].astype(str) < str(fit_before)]
        if df.empty:
            return None

    s = pd.to_numeric(df["watchability_score"], errors="coerce").to_numpy(dtype=float)
    y = (df["watched_within_h"].astype("boolean").fillna(False)
         .to_numpy(dtype=bool).astype(float))
    dates = (df["snapshot_date"].astype(str).to_numpy()
             if "snapshot_date" in df.columns else np.asarray([], dtype=object))
    row_src = (df["source"].fillna("prospective").replace("", "prospective")
               .astype(str).to_numpy()
               if "source" in df.columns
               else np.full(len(df), "prospective", dtype=object))
    ok = np.isfinite(s)
    s, y = s[ok], y[ok]
    if len(dates):
        dates = dates[ok]
    row_src = row_src[ok]
    if len(s) == 0:
        return None

    # Positives by provenance — a fresh install's evidence is entirely
    # reconstructed, and the report must never hide that behind one n_pos.
    pos = y > 0
    n_pos_by_source = {str(k): int(v) for k, v in
                       zip(*np.unique(row_src[pos], return_counts=True))} \
        if pos.any() else {}

    meta = {
        "fit_from": (str(min(dates)) if len(dates) else None),
        "fit_to": (str(max(dates)) if len(dates) else None),
        "sources": sources,
        "n_pos_by_source": n_pos_by_source,
    }
    return s, y, meta


def fit_calibrator(snapshots_df, horizon_days: int = DEFAULT_HORIZON_DAYS, *,
                   history=None, now=None, service: "str | None" = None,
                   include_backfill: bool = False,
                   fit_before: "str | None" = None,
                   require_gate: bool = False) -> "CalibrationResult | None":
    """Isotonic map ``watchability_score -> P(watch within horizon_days)``.

    ``snapshots_df`` is a ``labels/snapshots.load_snapshots`` frame. It may
    already carry ``watched_within_h`` / ``label_mature`` (e.g. straight out of
    ``build_labels``); otherwise pass ``history`` — the Tautulli events frame or
    a cache base dir — and the labels are built here with the same rules.

    Returns ``None`` — never a half-trusted object — when:
      * the frame is empty / missing ``watchability_score`` / has no labels and
        no ``history`` to build them from;
      * nothing has matured yet, or the ``fit_before`` split empties the window;
      * the score or the outcome is constant (isotonic has nothing to fit);
      * fewer than :data:`MIN_POS_FOR_FIT` positives survived — the hard "no
        evidence" floor; shrinkage cannot rescue a map that was never fittable;
      * the deduplicated map collapses to fewer than two knots.

    The §10 milestone is NOT one of those reasons any more: the result is
    stamped ``gate_ok`` / ``gate_reason`` for the report, and the caller shrinks
    the inverted cutoff toward the hand-set constant (:func:`blend_threshold`)
    in proportion to ``n_pos``. ``require_gate=True`` restores the old hard
    refusal below ``MIN_POS_FOR_DERIVATION`` for diagnostics and backtests.
    """
    prepared = _prepare_rows(snapshots_df, horizon_days, history=history, now=now,
                             service=service, include_backfill=include_backfill,
                             fit_before=fit_before)
    if prepared is None:
        return None
    s, y, meta = prepared

    n = int(len(s))
    n_pos = int(y.sum())
    gate_ok, gate_reason = data_gate(n_pos)
    if require_gate and not gate_ok:
        return None
    if n < 2 or n_pos < MIN_POS_FOR_FIT or n_pos == n:
        return None
    if float(np.nanmax(s)) <= float(np.nanmin(s)):
        return None

    bx, by = isotonic_fit(s, y)
    kx, ky = _dedupe_knots(np.asarray(bx, dtype=float), np.asarray(by, dtype=float))
    if len(kx) < 2:
        return None

    cal = CalibrationResult(
        knots_score=tuple(float(v) for v in kx),
        knots_p=tuple(float(v) for v in ky),
        horizon_days=int(horizon_days),
        n=n,
        n_pos=n_pos,
        base_rate=float(y.mean()),
        service=service,
        gate_ok=bool(gate_ok),
        gate_reason=gate_reason,
        fit_from=meta["fit_from"],
        fit_to=meta["fit_to"],
        fit_before=fit_before,
        include_backfill=bool(include_backfill),
        sources=meta["sources"],
        n_pos_by_source=meta["n_pos_by_source"],
    )
    try:
        ece = float(expected_calibration_error(y, cal.predict(s)))
    except Exception:
        ece = float("nan")
    return replace(cal, ece=ece)


# ── invert ────────────────────────────────────────────────────────────────────

def derive_threshold(calibrator: CalibrationResult, target_p: float, *,
                     lo: float = SCORE_MIN, hi: float = SCORE_MAX) -> float:
    """The score at which the calibrated probability crosses *target_p*.

    Formally the **lower (left-continuous) generalized inverse**

        s* = inf { s : F(s) >= target_p },      F = the isotonic map

    which is the only choice that makes the derived rule *equivalent* to the
    probability rule: ``score >= s*``  ⟺  ``F(score) >= target_p``. An isotonic
    map is a step function with plateaus, so its inverse is set-valued there;
    picking any point above the plateau's left edge would exclude scores whose
    estimated probability already meets the target, and the two rules would
    disagree. The cost is that the derived cutoff sits at the LEFT edge of the
    plateau containing the hand-set constant — on the real store, inverting
    P(score=20) returns 12, because at 38 positives the calibrator genuinely
    cannot distinguish 12 from 20. That gap is reported as a flip count by
    ``shadow.compare``; it is a statement about the data, not a bug.

    Between knots the map is piecewise-linear (``isotonic_predict`` interpolates),
    so the crossing is interpolated too — the derived cutoff is a float, not
    snapped to an observed score.

    Clamping (both documented, both tested):
      * ``target_p <= F(lowest knot)`` — every score already qualifies →
        returns ``lo`` (0.0).
      * ``target_p > F(highest knot)`` — no score qualifies; the map is clamped
        flat at the ends by construction (§5, no extrapolated probabilities) →
        returns ``hi`` (100.0), i.e. "nothing passes".
      * the result is always clamped into ``[lo, hi]``.

    Raises ``ValueError`` if *calibrator* is None or carries no knots — callers
    must handle the ``fit_calibrator`` → ``None`` case explicitly rather than
    silently deriving from nothing.
    """
    if calibrator is None or not getattr(calibrator, "knots_score", None):
        raise ValueError("derive_threshold requires a fitted CalibrationResult "
                         "(fit_calibrator returned None — check the data gate)")
    try:
        p = float(target_p)
    except (TypeError, ValueError):
        raise ValueError(f"target_p must be a probability, got {target_p!r}")
    if not np.isfinite(p):
        raise ValueError(f"target_p must be finite, got {target_p!r}")

    kx = np.asarray(calibrator.knots_score, dtype=float)
    ky = np.asarray(calibrator.knots_p, dtype=float)

    if p <= ky[0]:
        return float(min(max(lo, lo), hi))          # every score qualifies
    if p > ky[-1]:
        return float(hi)                            # no score qualifies

    j = int(np.searchsorted(ky, p, side="left"))    # first knot with ky[j] >= p
    j = min(j, len(ky) - 1)
    if j == 0:
        s = float(kx[0])
    else:
        x0, x1 = float(kx[j - 1]), float(kx[j])
        y0, y1 = float(ky[j - 1]), float(ky[j])
        if y1 <= y0 or x1 <= x0:
            s = float(x1)                           # vertical jump at x1
        else:
            s = x0 + (p - y0) * (x1 - x0) / (y1 - y0)
    return float(min(max(s, lo), hi))


# ── convenience for the report layer ──────────────────────────────────────────

def derive_all(calibrators: dict, specs, targets: dict) -> dict:
    """``{spec.name: RAW derived score | None}`` for every spec whose service has
    a calibrator. A spec whose service produced none (no labels, too few
    positives, degenerate map) maps to ``None`` — the caller then reports the
    reason and, via :func:`blend_threshold`, an effective value that IS the
    constant. The raw value is never what a consumer reads: shrink it first."""
    out: dict = {}
    for spec in specs:
        cal = (calibrators or {}).get(getattr(spec, "service", None))
        tgt = (targets or {}).get(getattr(spec, "bucket", None))
        if cal is None or tgt is None:
            out[spec.name] = None
            continue
        try:
            out[spec.name] = derive_threshold(cal, float(tgt))
        except (ValueError, TypeError):
            out[spec.name] = None
    return out


def reports_dir(base_dir) -> Path:
    """``<cache>/ml/reports`` — where the audit JSON lands (same dir the forward
    validation / weight refit tools already write to)."""
    return Path(base_dir) / "ml" / "reports"
