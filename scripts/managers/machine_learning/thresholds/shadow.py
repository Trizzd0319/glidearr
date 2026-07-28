"""
thresholds/shadow.py — "what would change if the derived cutoffs drove?" (pure).
================================================================================
The whole rollout rests on being able to answer that question BEFORE anything
changes, every run, for free. :func:`compare` is that answer: given the current
constants, the derived score-equivalents, and the library's scores, it counts —
per threshold — how many entities sit on each side of each cutoff and, crucially,
how many would **flip** (be treated differently) if the derived value drove.

PURE: no config, no I/O, no clock, no pandas requirement (numpy only, and only
for the counting). Everything it needs is passed in, which is what makes the
counts assertable against a hand-computed example in the tests.

RAW vs EFFECTIVE. Since the §10 gate became empirical-Bayes shrinkage
(``derive.blend_threshold``), two numbers matter per threshold: the RAW
``derived`` cutoff the calibrator inverted, and the ``effective`` one a consumer
in ``mode="derived"`` would actually read — ``w·derived + (1−w)·constant``. All
COUNTING here is done on the effective value, because that is the partition the
household would experience; the raw value rides along for the report so the
reader can see how far the shrinkage pulled it back. Callers that pass no
``effective`` mapping get the old behaviour (effective = derived).

Convention: a threshold partitions entities at ``score >= t``. Every routed
cutoff is one of two shapes — "act at/above" (monitor, acquire, 4K-eligible) or
"act below" (delete, demote, dormant) — but both induce the SAME partition, so
the flip count is identical either way and is reported once. ``direction`` says
which way the EFFECTIVE cutoff moves the line:

    "looser"     effective < current  → MORE entities at/above (more acquires,
                                        fewer deletes)
    "stricter"   effective > current  → FEWER entities at/above
    "identical"  the same partition of these scores
"""
from __future__ import annotations

import numpy as np

_EPS = 1e-9


def _scores_for(scores, name) -> np.ndarray:
    """``scores`` may be one sequence (used for every threshold) or a mapping
    keyed by threshold name / service — missing keys give an empty array."""
    if scores is None:
        return np.asarray([], dtype=float)
    if isinstance(scores, dict):
        raw = scores.get(name, [])
    else:
        raw = scores
    arr = np.asarray(list(raw), dtype=float) if not isinstance(raw, np.ndarray) \
        else raw.astype(float)
    return arr[np.isfinite(arr)] if arr.size else arr.reshape(0)


def _as_float(v):
    """float(v) or None — never raises, never returns a NaN/inf."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if np.isfinite(f) else None


def _side_counts(arr: np.ndarray, t: float) -> "tuple[int, int]":
    if arr.size == 0:
        return 0, 0
    above = int(np.count_nonzero(arr >= t - _EPS))
    return above, int(arr.size) - above


def compare(current_constants, derived, scores, *, effective=None, weights=None,
            calibrated_p_at_current=None, target_p=None,
            score_key=None) -> dict:
    """Shadow comparison of hand-set vs derived/effective cutoffs.

    Parameters
    ----------
    current_constants : mapping ``name -> float``
        Today's literal for each threshold (what the consumer actually uses).
    derived : mapping ``name -> float | None``
        The RAW calibrated score-equivalent. ``None`` = no derived value at all
        (no calibrator for that service: no labels, too few positives, or a
        degenerate map) — the row is reported with ``status="no_derived"``,
        ``effective`` falls back to the constant, and it contributes zero flips.
    scores : sequence | mapping
        The scored population. A mapping is looked up by threshold name first
        and then by ``score_key[name]`` (typically the service), so movie
        thresholds can be compared against movie scores and series thresholds
        against series scores in one call.
    effective : mapping ``name -> float | None``, optional
        The SHRUNK cutoff (``derive.blend_threshold``) — what ``mode="derived"``
        would really use, and what every count below is computed on. Omit it and
        the raw derived value is used, which is what pre-shrinkage callers and
        the hand-computed unit examples expect.
    weights : mapping ``name -> float``, optional
        The shrinkage weight ``w = n_pos/(n_pos+k)`` behind each effective
        value; carried through to the report, used in no count.
    calibrated_p_at_current : mapping ``name -> float | None``, optional
        P(watch within H) that the calibrator assigns to the CURRENT constant —
        the "what does 35 actually mean today" column of the report.
    target_p : mapping ``name -> float | None``, optional
        The bucket target the derived value was inverted from (carried through
        for the report; not used in any count).
    score_key : mapping ``name -> str``, optional
        Secondary lookup key into ``scores`` (e.g. ``{"series_monitor":
        "sonarr"}``).

    Returns
    -------
    dict with ``thresholds`` (name -> row), ``totals`` and ``n_compared``.
    Each row: current, derived (raw), w, effective, delta (effective−current),
    delta_raw, target_p, p_at_current, n, n_at_or_above/n_below for current and
    effective, n_flip, flip_pct, direction, status.
    """
    current_constants = dict(current_constants or {})
    derived = dict(derived or {})
    eff_in = dict(effective or {})
    w_in = dict(weights or {})
    p_at_cur = dict(calibrated_p_at_current or {})
    targets = dict(target_p or {})
    keys = dict(score_key or {})

    rows: dict = {}
    total_flip = 0
    n_derived = 0
    n_no_derived = 0

    for name in current_constants:
        cur = current_constants.get(name)
        arr = _scores_for(scores, name)
        if arr.size == 0 and isinstance(scores, dict) and name in keys:
            arr = _scores_for(scores, keys[name])

        try:
            cur_f = float(cur)
        except (TypeError, ValueError):
            cur_f = float("nan")

        cur_above, cur_below = _side_counts(arr, cur_f) if np.isfinite(cur_f) else (0, 0)
        d = derived.get(name)
        # No shrinkage supplied → effective IS the raw value (pre-blend callers).
        e = eff_in.get(name, d) if eff_in else d
        row = {
            "current": cur,
            "derived": None,
            "w": _as_float(w_in.get(name)) if d is not None else None,
            "effective": _as_float(e) if e is not None else (
                cur_f if np.isfinite(cur_f) else None),
            "delta": None,
            "delta_raw": None,
            "target_p": targets.get(name),
            "p_at_current": p_at_cur.get(name),
            "n": int(arr.size),
            "n_at_or_above_current": cur_above,
            "n_below_current": cur_below,
            "n_at_or_above_effective": None,
            "n_below_effective": None,
            "n_flip": 0,
            "flip_pct": None,
            "direction": "none",
            "status": "no_derived",
        }

        d_f = _as_float(d)
        e_f = _as_float(e)
        if d is None or d_f is None or e_f is None or not np.isfinite(cur_f):
            # No derived term: `blend_threshold` returns the constant itself, so
            # the effective partition IS the current one and nothing flips —
            # reported, not hidden, and the "effective" column still shows a
            # real number (the constant) rather than a dash.
            if np.isfinite(cur_f):
                if row["effective"] is None:
                    row["effective"] = cur_f
                eff_above, eff_below = _side_counts(arr, float(row["effective"]))
                row["n_at_or_above_effective"] = eff_above
                row["n_below_effective"] = eff_below
                row["delta"] = float(row["effective"]) - cur_f
            n_no_derived += 1
            rows[name] = row
            continue

        n_derived += 1
        e_above, e_below = _side_counts(arr, e_f)
        if arr.size:
            flip = int(np.count_nonzero((arr >= cur_f - _EPS) != (arr >= e_f - _EPS)))
        else:
            flip = 0
        total_flip += flip

        if abs(e_f - cur_f) <= _EPS:
            direction = "identical"
        elif e_f < cur_f:
            direction = "looser"
        else:
            direction = "stricter"
        if flip == 0 and direction != "identical":
            direction += "_no_effect"

        row.update({
            "derived": d_f,
            "effective": e_f,
            "delta": e_f - cur_f,
            "delta_raw": d_f - cur_f,
            "n_at_or_above_effective": e_above,
            "n_below_effective": e_below,
            "n_flip": flip,
            "flip_pct": (round(100.0 * flip / arr.size, 2) if arr.size else None),
            "direction": direction,
            "status": "ok",
        })
        rows[name] = row

    return {
        "thresholds": rows,
        "totals": {
            "n_thresholds": len(rows),
            "n_with_derived": n_derived,
            "n_gated_out": n_no_derived,
            "n_flip_total": total_flip,
        },
        "n_compared": sum(r["n"] for r in rows.values()),
    }


def render_rows(comparison: dict, *, specs_by_name=None,
                n_pos_by_name=None, n_pos_split_by_name=None,
                confidence_by_name=None) -> list:
    """``compare`` output → the end-of-run log grid's rows.

    One row per threshold: ``[name, current, derived, w, effective, n_pos
    (p+b), confidence, would-flip]`` — the whole blend at a glance. ``derived``
    is the RAW inverted cutoff and ``effective`` is what ``mode="derived"``
    would use, so the reader can see the shrinkage happen. A threshold with no
    derived value prints ``-`` for derived/w, ``effective`` equal to its
    constant, and ``none`` for confidence: the table never implies a number it
    does not have, and never hides the one it would act on.

    ``n_pos`` is printed with its PROVENANCE — ``38 (12p+26b)`` = 12 prospective
    positives + 26 reconstructed by the backfill — because on a fresh install
    the evidence is entirely reconstructed and that must be visible in the same
    glance as the value it moved.
    """
    n_pos_by_name = n_pos_by_name or {}
    splits = n_pos_split_by_name or {}
    confidences = confidence_by_name or {}
    specs_by_name = specs_by_name or {}

    def _fmt_s(v):
        if v is None:
            return "-"
        try:
            return f"{float(v):.1f}"
        except (TypeError, ValueError):
            return "-"

    def _fmt_w(v):
        if v is None:
            return "-"
        try:
            return f"{float(v):.2f}"
        except (TypeError, ValueError):
            return "-"

    def _fmt_npos(name) -> str:
        total = n_pos_by_name.get(name)
        split = splits.get(name) or {}
        pro = split.get("prospective")
        bf = split.get("backfill")
        if total is None:
            return "-"
        if pro is None and bf is None:
            return str(total)
        return f"{int(total)} ({int(pro or 0)}p+{int(bf or 0)}b)"

    out: list = []
    for name in sorted(comparison.get("thresholds", {})):
        row = comparison["thresholds"][name]
        spec = specs_by_name.get(name)
        if row.get("status") != "ok":
            flip = "-"
        else:
            d = row.get("direction", "")
            flip = f"{row.get('n_flip', 0)}/{row.get('n', 0)}"
            if row.get("n_flip"):
                flip += f" ({d.replace('_no_effect', '')})"
        out.append([
            name if spec is None or spec.routed else f"{name} *",
            _fmt_s(row.get("current")),
            _fmt_s(row.get("derived")),
            _fmt_w(row.get("w")),
            _fmt_s(row.get("effective")),
            _fmt_npos(name),
            str(confidences.get(name, "none"))[:20],
            flip,
        ])
    return out


RENDER_HEADERS = ["threshold", "current", "derived", "w", "effective",
                  "n_pos (p+b)", "confidence", "would flip"]
