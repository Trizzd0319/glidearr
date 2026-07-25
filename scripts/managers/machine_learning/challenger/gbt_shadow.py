"""
challenger/gbt_shadow.py — shadow GBT watch-probability challenger (ML Stage 4).
================================================================================
A LightGBM classifier trained OFFLINE (scripts/support/tools/ml_train_challenger.py)
on the Stage-1 labeled snapshots: sig_* signal-group contributions + a few
context columns → P(watched within horizon). Calibrated with pure-numpy
isotonic regression (eval/np_metrics PAVA) fitted on the temporal validation
window, so the persisted P is an honest probability, not a raw margin.

SHADOW-ONLY, BY CONSTRUCTION:
  * runtime entry point is :func:`attach_challenger_p`, called from the snapshot
    hook (labels/snapshots) AFTER the score map is built and persisted — it can
    only (a) log divergence between the hand-weighted score and P, and (b) fill
    the ``challenger_p`` column of the snapshot rows. Nothing downstream reads
    challenger_p at runtime; no score, decision, or *arr write can change.
  * config gate: ``scoring.ml_challenger.enabled`` — DEFAULT FALSE. Even when
    true, a missing model file → silent no-op.
  * lightgbm is OPTIONAL. Absent → the module logs ONE line (per process) the
    first time the enabled path is hit and no-ops. Nothing hard-requires it.

Artifacts (under <cache>/ml/models/):
    gbt_challenger_{service}.txt         LightGBM Booster (text format)
    gbt_challenger_{service}.calib.json  {features, calib_bx, calib_by, trained_at,
                                          metrics, n_train, n_valid}
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.managers.machine_learning.eval.np_metrics import (
    average_precision,
    brier_score,
    isotonic_fit,
    isotonic_predict,
    spearman_rho,
)

# ── optional dependency ───────────────────────────────────────────────────────
try:
    import lightgbm as _lgb           # type: ignore
    HAS_LIGHTGBM = True
except Exception:                      # ImportError or any binary-load failure
    _lgb = None
    HAS_LIGHTGBM = False

_ABSENT_LOGGED = False                 # one line per process, not one per pass
_MODEL_CACHE: dict = {}                # (path, mtime) -> bundle

MODELS_SUBDIR = ("ml", "models")
_CONTEXT_FEATURES = ["size_gb", "resolution", "watched_before", "in_up_next"]


def models_dir(base_dir) -> Path:
    return Path(base_dir).joinpath(*MODELS_SUBDIR)


def model_paths(base_dir, service: str) -> "tuple[Path, Path]":
    d = models_dir(base_dir)
    return d / f"gbt_challenger_{service}.txt", d / f"gbt_challenger_{service}.calib.json"


# ── feature plumbing (pure) ───────────────────────────────────────────────────

def feature_columns(df: pd.DataFrame) -> list[str]:
    """Deterministic training feature list: every sig_* column + context."""
    cols = sorted(c for c in df.columns if c.startswith("sig_"))
    for c in _CONTEXT_FEATURES:
        if c == "size_gb" or c in df.columns:
            cols.append(c)
    return cols


def build_feature_matrix(rows, feat_cols: list[str]) -> np.ndarray:
    """(n, d) float matrix from row dicts or a DataFrame; missing → 0.0.
    ``size_gb`` is derived from ``size_bytes`` so runtime rows need no extra key."""
    if isinstance(rows, pd.DataFrame):
        records = rows.to_dict("records")
    else:
        records = list(rows)
    X = np.zeros((len(records), len(feat_cols)), dtype=float)
    for i, rec in enumerate(records):
        for j, col in enumerate(feat_cols):
            if col == "size_gb":
                v = rec.get("size_bytes")
                try:
                    X[i, j] = float(v) / (1024 ** 3) if v is not None and not pd.isna(v) else 0.0
                except (TypeError, ValueError):
                    X[i, j] = 0.0
                continue
            v = rec.get(col)
            if v is None or (isinstance(v, float) and pd.isna(v)):
                X[i, j] = 0.0
            elif isinstance(v, bool):
                X[i, j] = 1.0 if v else 0.0
            else:
                try:
                    X[i, j] = float(v)
                except (TypeError, ValueError):
                    X[i, j] = 0.0
    return X


# ── training (CLI-only; never called from the run) ────────────────────────────

def train_challenger(labeled_df: pd.DataFrame, service: str, base_dir, *,
                     split_date: "str | None" = None,
                     num_boost_round: int = 400,
                     early_stopping_rounds: int = 30,
                     params: "dict | None" = None,
                     logger=None) -> "dict | None":
    """Train + calibrate + persist the challenger for one service.

    Temporal split: train < split_date <= valid. Without a split date the last
    ~20% of DISTINCT snapshot days become validation; a single-day dataset falls
    back to a warned random 80/20 (degenerate — labels are in-sample).
    Returns the report dict, or None when untrainable (no lightgbm / no
    positives / too few rows)."""
    if not HAS_LIGHTGBM:
        _log(logger, "warning", "[Challenger] lightgbm not installed — cannot train.")
        return None
    df = labeled_df[labeled_df["service"] == service].reset_index(drop=True)
    if df.empty or "watched_within_h" not in df.columns:
        _log(logger, "warning", f"[Challenger] no labeled rows for {service}.")
        return None
    y = df["watched_within_h"].astype(float).to_numpy()
    if y.sum() == 0:
        _log(logger, "warning", f"[Challenger] zero positive labels for {service} — not training.")
        return None

    days = sorted(df["snapshot_date"].astype(str).unique())
    degenerate = False
    if split_date:
        train_mask = (df["snapshot_date"].astype(str) < split_date).to_numpy()
    elif len(days) >= 2:
        cut = days[max(1, int(round(len(days) * 0.8))) - 1]
        train_mask = (df["snapshot_date"].astype(str) <= cut).to_numpy()
    else:
        rng = np.random.default_rng(42)
        train_mask = rng.random(len(df)) < 0.8
        degenerate = True
    valid_mask = ~train_mask
    if train_mask.sum() < 20 or valid_mask.sum() < 5 or y[train_mask].sum() == 0:
        # fall back to a warned random split before giving up entirely
        rng = np.random.default_rng(42)
        train_mask = rng.random(len(df)) < 0.8
        valid_mask = ~train_mask
        degenerate = True
        if train_mask.sum() < 20 or y[train_mask].sum() == 0:
            _log(logger, "warning",
                 f"[Challenger] {service}: too little data to train "
                 f"(n={len(df)}, positives={int(y.sum())}).")
            return None

    feat_cols = feature_columns(df)
    X = build_feature_matrix(df, feat_cols)
    lgb_params = {
        "objective": "binary",
        "metric": "average_precision",
        "learning_rate": 0.05,
        "num_leaves": 15,          # small — n is tiny, deep trees would memorise
        "min_data_in_leaf": 10,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
        "verbosity": -1,
    }
    if params:
        lgb_params.update(params)
    dtrain = _lgb.Dataset(X[train_mask], label=y[train_mask], feature_name=feat_cols)
    dvalid = _lgb.Dataset(X[valid_mask], label=y[valid_mask], reference=dtrain,
                          feature_name=feat_cols)
    booster = _lgb.train(
        lgb_params, dtrain, num_boost_round=num_boost_round,
        valid_sets=[dvalid],
        callbacks=[_lgb.early_stopping(early_stopping_rounds, verbose=False),
                   _lgb.log_evaluation(period=0)],
    )
    raw_valid = booster.predict(X[valid_mask], num_iteration=booster.best_iteration)
    bx, by = isotonic_fit(raw_valid, y[valid_mask])
    cal_valid = isotonic_predict(bx, by, raw_valid)

    metrics = {
        "valid_auc_pr": _round4(average_precision(y[valid_mask], raw_valid)),
        "valid_base_rate": _round4(float(y[valid_mask].mean())),
        "valid_brier_raw": _round4(brier_score(y[valid_mask], raw_valid)),
        "valid_brier_calibrated": _round4(brier_score(y[valid_mask], cal_valid)),
        "best_iteration": int(booster.best_iteration or 0),
        "degenerate_split": bool(degenerate),
    }
    model_path, calib_path = model_paths(base_dir, service)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(model_path), num_iteration=booster.best_iteration)
    calib_path.write_text(json.dumps({
        "features": feat_cols,
        "calib_bx": [float(v) for v in bx],
        "calib_by": [float(v) for v in by],
        "trained_at": datetime.now(tz=timezone.utc).isoformat(),
        "split_date": split_date,
        "n_train": int(train_mask.sum()),
        "n_valid": int(valid_mask.sum()),
        "n_pos_total": int(y.sum()),
        "metrics": metrics,
    }, indent=2), encoding="utf-8")
    return {"model_path": str(model_path), "calib_path": str(calib_path),
            "features": feat_cols, "metrics": metrics,
            "n_train": int(train_mask.sum()), "n_valid": int(valid_mask.sum())}


# ── runtime shadow path (observe-only) ────────────────────────────────────────

def load_challenger(base_dir, service: str):
    """Load booster + calibration (mtime-cached per process). None on any miss."""
    if not HAS_LIGHTGBM:
        return None
    model_path, calib_path = model_paths(base_dir, service)
    if not model_path.is_file() or not calib_path.is_file():
        return None
    try:
        key = (str(model_path), os.path.getmtime(model_path))
        cached = _MODEL_CACHE.get(key)
        if cached is not None:
            return cached
        calib = json.loads(calib_path.read_text(encoding="utf-8"))
        bundle = {
            "booster": _lgb.Booster(model_file=str(model_path)),
            "features": list(calib.get("features") or []),
            "calib_bx": np.asarray(calib.get("calib_bx") or [], dtype=float),
            "calib_by": np.asarray(calib.get("calib_by") or [], dtype=float),
            "trained_at": calib.get("trained_at"),
        }
        _MODEL_CACHE.clear()           # keep at most one model per service alive
        _MODEL_CACHE[key] = bundle
        return bundle
    except Exception:
        return None


def predict_probability(bundle, rows) -> np.ndarray:
    """Calibrated P(watch within horizon) for snapshot rows (dicts or a df)."""
    X = build_feature_matrix(rows, bundle["features"])
    raw = bundle["booster"].predict(X)
    if len(bundle["calib_bx"]):
        return np.clip(isotonic_predict(bundle["calib_bx"], bundle["calib_by"], raw), 0.0, 1.0)
    return np.clip(np.asarray(raw, dtype=float), 0.0, 1.0)


def divergence_report(rows, ps: np.ndarray, top_n: int = 10) -> dict:
    """Rank disagreement between the hand score and the challenger P (pure).

    Returns {"rho": spearman, "top_disagreements": [{title, entity_id, score,
    challenger_p, rank_score, rank_p, rank_delta}]} — the entities the two
    rankers disagree about hardest, largest |Δrank| first."""
    scores = np.array([float(r.get("watchability_score") or 0.0) for r in rows])
    n = len(scores)
    if n == 0:
        return {"rho": None, "top_disagreements": []}
    order_s = np.argsort(np.argsort(-scores, kind="stable"), kind="stable")   # 0 = best score
    order_p = np.argsort(np.argsort(-ps, kind="stable"), kind="stable")       # 0 = highest P
    delta = np.abs(order_s - order_p)
    top_idx = np.argsort(-delta, kind="stable")[:top_n]
    tops = []
    for i in top_idx:
        if delta[i] == 0:
            continue
        tops.append({
            "title": rows[i].get("title"),
            "entity_id": rows[i].get("entity_id"),
            "score": scores[i],
            "challenger_p": round(float(ps[i]), 4),
            "rank_score": int(order_s[i]) + 1,
            "rank_p": int(order_p[i]) + 1,
            "rank_delta": int(delta[i]),
        })
    rho = spearman_rho(scores, ps)
    return {"rho": round(float(rho), 4) if np.isfinite(rho) else None,
            "top_disagreements": tops}


def _challenger_enabled(config) -> bool:
    """config.scoring.ml_challenger.enabled — DEFAULT FALSE."""
    sc = ((config or {}).get("scoring", {}) or {})
    mc = (sc.get("ml_challenger", {}) or {}) if isinstance(sc, dict) else {}
    return bool(mc.get("enabled", False)) if isinstance(mc, dict) else False


def attach_challenger_p(config, logger, base_dir, service: str, rows: list) -> list:
    """Runtime shadow hook (called from labels/snapshots after each score-map
    build). Observe-only: fills ``challenger_p`` in the snapshot rows and logs
    divergence. Disabled (default) / no model / no lightgbm → rows unchanged.
    Never raises."""
    global _ABSENT_LOGGED
    try:
        if not rows or not _challenger_enabled(config):
            return rows
        if not HAS_LIGHTGBM:
            if not _ABSENT_LOGGED:
                _ABSENT_LOGGED = True
                _log(logger, "info",
                     "[Challenger] scoring.ml_challenger.enabled is true but lightgbm "
                     "is not installed — shadow challenger no-ops (pip install lightgbm).")
            return rows
        bundle = load_challenger(base_dir, service)
        if bundle is None:
            return rows
        ps = predict_probability(bundle, rows)
        for r, p in zip(rows, ps):
            r["challenger_p"] = round(float(p), 4)
        rep = divergence_report(rows, ps, top_n=10)
        _log(logger, "info",
             f"[Challenger] {service}: shadow P(watch) for {len(rows)} entities — "
             f"Spearman rho(score, P) = {rep['rho']} (model {bundle.get('trained_at')}). "
             f"Observe-only: nothing reads challenger_p at runtime.")
        for d in rep["top_disagreements"]:
            _log(logger, "info",
                 f"[Challenger]   rank disagreement: {d['title']!r} "
                 f"score={d['score']:.0f} (rank {d['rank_score']}) vs "
                 f"P={d['challenger_p']:.2f} (rank {d['rank_p']}) — Δ{d['rank_delta']}")
        return rows
    except Exception as e:
        _log(logger, "debug", f"[Challenger] shadow pass skipped: {e}")
        return rows


# ── tiny logging shim (never raises) ──────────────────────────────────────────

def _log(logger, level: str, msg: str) -> None:
    try:
        getattr(logger, f"log_{level}")(msg)
    except Exception:
        pass


def _round4(v):
    try:
        f = float(v)
        return round(f, 4) if np.isfinite(f) else None
    except (TypeError, ValueError):
        return None
