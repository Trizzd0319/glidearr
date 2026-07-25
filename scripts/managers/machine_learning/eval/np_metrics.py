"""
eval/np_metrics.py — pure-numpy score-array metrics (no sklearn dependency).
================================================================================
Shared by the offline ML tools (ml_forward_validation / ml_weight_refit /
ml_train_challenger). sklearn is NOT installed in this environment, so the
classics are implemented directly:

  * average_precision(y_true, y_score)      AUC-PR (sklearn-compatible AP:
                                            sum over positives of precision at
                                            each recall step)
  * brier_score(y_true, p)                  mean squared error of probabilities
  * minmax_scale(x)                         [0,1] scaling (constant vector -> 0.5)
  * calibration_table(y_true, p, bins)      per-bin mean prediction vs empirical rate
  * spearman_rho(a, b)                      rank correlation (average-rank ties)
  * isotonic_fit(x, y) / isotonic_predict   PAVA monotone regression — the
                                            challenger's probability calibration
  * logistic_irls(X, y, l2)                 L2-regularised logistic regression via
                                            iteratively-reweighted least squares

Pure functions over numpy arrays — no I/O, no manager graph, unit-testable.
"""
from __future__ import annotations

import numpy as np


def _as1d(a) -> np.ndarray:
    return np.asarray(a, dtype=float).ravel()


def average_precision(y_true, y_score) -> float:
    """AUC-PR as average precision (matches sklearn.average_precision_score).

    NaN when there are no positives (undefined). Ties are broken by stable sort
    order, matching the step-function AP definition."""
    y = _as1d(y_true)
    s = _as1d(y_score)
    mask = ~(np.isnan(y) | np.isnan(s))
    y, s = y[mask], s[mask]
    n_pos = float(y.sum())
    if len(y) == 0 or n_pos == 0:
        return float("nan")
    order = np.argsort(-s, kind="stable")
    y_sorted = y[order]
    cum_tp = np.cumsum(y_sorted)
    precision = cum_tp / np.arange(1, len(y_sorted) + 1)
    # AP = sum of precision at each positive, / n_pos
    return float((precision * y_sorted).sum() / n_pos)


def brier_score(y_true, p) -> float:
    """Mean squared error between outcomes {0,1} and predicted probabilities."""
    y = _as1d(y_true)
    q = _as1d(p)
    mask = ~(np.isnan(y) | np.isnan(q))
    if not mask.any():
        return float("nan")
    return float(np.mean((q[mask] - y[mask]) ** 2))


def minmax_scale(x) -> np.ndarray:
    """Scale to [0,1]; a constant vector maps to 0.5 everywhere (no information)."""
    v = _as1d(x)
    lo, hi = np.nanmin(v), np.nanmax(v)
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return np.full_like(v, 0.5)
    return (v - lo) / (hi - lo)


def calibration_table(y_true, p, bins: int = 10) -> list[dict]:
    """Equal-width probability bins: [{bin, lo, hi, n, mean_pred, watch_rate}]."""
    y = _as1d(y_true)
    q = _as1d(p)
    mask = ~(np.isnan(y) | np.isnan(q))
    y, q = y[mask], q[mask]
    edges = np.linspace(0.0, 1.0, bins + 1)
    out: list[dict] = []
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        sel = (q >= lo) & (q < hi) if i < bins - 1 else (q >= lo) & (q <= hi)
        n = int(sel.sum())
        out.append({
            "bin": i,
            "lo": round(float(lo), 3),
            "hi": round(float(hi), 3),
            "n": n,
            "mean_pred": round(float(q[sel].mean()), 4) if n else None,
            "watch_rate": round(float(y[sel].mean()), 4) if n else None,
        })
    return out


def _rank_average(v: np.ndarray) -> np.ndarray:
    """Average ranks (1-based) with ties sharing the mean rank."""
    order = np.argsort(v, kind="stable")
    ranks = np.empty(len(v), dtype=float)
    ranks[order] = np.arange(1, len(v) + 1, dtype=float)
    # average tied groups
    sv = v[order]
    i = 0
    while i < len(sv):
        j = i
        while j + 1 < len(sv) and sv[j + 1] == sv[i]:
            j += 1
        if j > i:
            ranks[order[i:j + 1]] = ranks[order[i:j + 1]].mean()
        i = j + 1
    return ranks


def spearman_rho(a, b) -> float:
    """Spearman rank correlation with average-rank tie handling."""
    x = _as1d(a)
    y = _as1d(b)
    mask = ~(np.isnan(x) | np.isnan(y))
    x, y = x[mask], y[mask]
    if len(x) < 2:
        return float("nan")
    rx, ry = _rank_average(x), _rank_average(y)
    sx, sy = rx.std(), ry.std()
    if sx == 0 or sy == 0:
        return float("nan")
    return float(np.corrcoef(rx, ry)[0, 1])


# ── isotonic regression (PAVA) — the challenger's probability calibration ─────

def isotonic_fit(x, y) -> "tuple[np.ndarray, np.ndarray]":
    """Pool-adjacent-violators: fit a non-decreasing step function y ~ f(x).

    Returns (bx, by): breakpoint inputs (sorted unique x of the pooled blocks)
    and fitted values. Use :func:`isotonic_predict` to interpolate."""
    xs = _as1d(x)
    ys = _as1d(y)
    mask = ~(np.isnan(xs) | np.isnan(ys))
    xs, ys = xs[mask], ys[mask]
    if len(xs) == 0:
        return np.array([0.0, 1.0]), np.array([0.5, 0.5])
    order = np.argsort(xs, kind="stable")
    xs, ys = xs[order], ys[order]
    # blocks: (sum_y, weight, x_last)
    vals = list(ys.astype(float))
    wts = [1.0] * len(vals)
    means = vals[:]
    i = 0
    # classic stack-based PAVA
    stack_val: list[float] = []
    stack_w: list[float] = []
    stack_end: list[int] = []
    for i in range(len(means)):
        cur_v, cur_w = means[i] * 1.0, 1.0
        while stack_val and stack_val[-1] / stack_w[-1] >= cur_v / cur_w:
            cur_v += stack_val.pop()
            cur_w += stack_w.pop()
            stack_end.pop()
        stack_val.append(cur_v)
        stack_w.append(cur_w)
        stack_end.append(i)
    fitted = np.empty(len(means))
    start = 0
    for v, w, end in zip(stack_val, stack_w, stack_end):
        fitted[start:end + 1] = v / w
        start = end + 1
    return xs, fitted


def isotonic_predict(bx: np.ndarray, by: np.ndarray, x_new) -> np.ndarray:
    """Piecewise-linear interpolation of the PAVA fit, clamped at the ends."""
    xn = _as1d(x_new)
    bx = np.asarray(bx, dtype=float)
    by = np.asarray(by, dtype=float)
    if len(bx) == 0:
        return np.full_like(xn, 0.5)
    return np.interp(xn, bx, by, left=float(by[0]), right=float(by[-1]))


# ── logistic regression via IRLS with L2 (Stage 3's refit core) ───────────────

def logistic_irls(X, y, l2: float = 1.0, max_iter: int = 50,
                  tol: float = 1e-8) -> "tuple[np.ndarray, float, bool]":
    """L2-regularised logistic regression (intercept unpenalised).

    X: (n, d) feature matrix (caller standardises), y: (n,) in {0,1}.
    Returns (beta, intercept, converged). Pure numpy IRLS; the ridge term keeps
    the Hessian invertible even with separable/sparse signal columns."""
    Xm = np.asarray(X, dtype=float)
    yv = _as1d(y)
    n, d = Xm.shape
    Xa = np.hstack([np.ones((n, 1)), Xm])          # column 0 = intercept
    beta = np.zeros(d + 1)
    R = np.eye(d + 1) * float(l2)
    R[0, 0] = 0.0                                  # never shrink the intercept
    converged = False
    for _ in range(max_iter):
        eta = Xa @ beta
        eta = np.clip(eta, -30, 30)
        p = 1.0 / (1.0 + np.exp(-eta))
        W = p * (1 - p)
        W = np.maximum(W, 1e-10)
        # Newton step: (X'WX + R) delta = X'(y - p) - R beta
        H = (Xa * W[:, None]).T @ Xa + R
        g = Xa.T @ (yv - p) - R @ beta
        try:
            delta = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:
            delta = np.linalg.lstsq(H, g, rcond=None)[0]
        beta = beta + delta
        if float(np.max(np.abs(delta))) < tol:
            converged = True
            break
    return beta[1:], float(beta[0]), converged
