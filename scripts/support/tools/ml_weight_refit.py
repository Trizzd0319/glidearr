"""
ml_weight_refit.py — refit the per-signal-group weights against real outcomes.
(ML Stage 3 — offline, read-only, NEVER auto-applied)
================================================================================
Standalone CLI (never imported by main.py). The watchability score is a
hand-weighted sum of independently-capped signal groups (sig_* columns in the
Stage-1 snapshots). This tool asks: with the labels we now have, what RELATIVE
per-group weights would a logistic regression choose?

Method (classical on purpose — n≈931 watch events household-wide):
  * features = the sig_* point contributions per snapshot row
  * target   = watched_within_h (labels/labeling.build_labels)
  * temporal split: train < --split-date <= test (degenerate → in-sample, warned)
  * standardize features on TRAIN, fit pure-numpy IRLS logistic with L2
    (--l2, default 1.0; intercept unpenalised)
  * per-point effect  w_j = beta_j / sd_j   (log-odds per score point of group j)
  * suggested multiplier m_j = w_j / max_k |w_k|  — normalized so the LARGEST
    fitted multiplier is 1.0, directly comparable to today's implicit 1.0 per
    group. A group at 0.3 means "its points earn ~30% of the top group's
    evidence"; negative means its points correlate with NOT watching.

The suggestions are written to <cache>/ml/reports/suggested_weights_{date}.json
and printed as a comparison table. NOTHING is applied anywhere — the scorers'
weights live in movie_scorer/show_scorer and only a human edits them.

Usage
-----
    python scripts/support/tools/ml_weight_refit.py
    python scripts/support/tools/ml_weight_refit.py --split-date 2026-08-01 --l2 2.0
    python scripts/support/tools/ml_weight_refit.py --service radarr --horizon-days 7
    python scripts/support/tools/ml_weight_refit.py --include-immature --no-write
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

import numpy as np   # noqa: E402
import pandas as pd  # noqa: E402

from scripts.managers.factories.cache.key_builder import CacheKeyBuilder      # noqa: E402
from scripts.managers.machine_learning.eval.np_metrics import (               # noqa: E402
    average_precision,
    logistic_irls,
)
from scripts.managers.machine_learning.labels.labeling import build_labels    # noqa: E402
from scripts.managers.machine_learning.labels.snapshots import load_snapshots # noqa: E402


def refit_service(df: pd.DataFrame, split_date: "str | None", l2: float,
                  warnings: list[str], service: str) -> "dict | None":
    """Fit + evaluate one service. Returns the report block (None when no data)."""
    sig_cols = sorted(c for c in df.columns if c.startswith("sig_"))
    if not sig_cols or df.empty:
        return None
    y_all = df["watched_within_h"].astype(float).to_numpy()

    mode = "forward_holdout"
    if split_date:
        train_mask = (df["snapshot_date"] < split_date).to_numpy()
        test_mask = ~train_mask
        if train_mask.sum() == 0 or test_mask.sum() == 0 or y_all[train_mask].sum() == 0:
            warnings.append(
                f"{service}: degenerate split at {split_date} "
                f"(train={int(train_mask.sum())}, test={int(test_mask.sum())}, "
                f"train positives={int(y_all[train_mask].sum())}) — fitting IN-SAMPLE.")
            mode = "in_sample_degenerate"
            train_mask = test_mask = np.ones(len(df), dtype=bool)
    else:
        warnings.append(f"{service}: no --split-date — fitting IN-SAMPLE "
                        "(same rows train and evaluate; optimistic by construction).")
        mode = "in_sample"
        train_mask = test_mask = np.ones(len(df), dtype=bool)

    X_raw = df[sig_cols].apply(pd.to_numeric, errors="coerce").fillna(0.0).to_numpy(dtype=float)
    n_pos_train = int(y_all[train_mask].sum())
    if n_pos_train == 0:
        warnings.append(f"{service}: zero positive labels — nothing to fit.")
        return None

    # Drop zero-variance columns (signals that never fire in train) from the fit.
    mu = X_raw[train_mask].mean(axis=0)
    sd = X_raw[train_mask].std(axis=0)
    active = sd > 1e-12
    dropped = [c for c, a in zip(sig_cols, active) if not a]
    fit_cols = [c for c, a in zip(sig_cols, active) if a]
    Z = (X_raw[:, active] - mu[active]) / sd[active]

    beta, intercept, converged = logistic_irls(Z[train_mask], y_all[train_mask], l2=l2)
    per_point = beta / sd[active]                    # log-odds per raw score point
    denom = float(np.max(np.abs(per_point))) if len(per_point) else 0.0

    weights: list[dict] = []
    m_by_col: dict = {}
    for j, col in enumerate(fit_cols):
        m = float(per_point[j] / denom) if denom > 0 else 0.0
        m_by_col[col] = m
        weights.append({
            "signal": col,
            "current_multiplier": 1.0,
            "fitted_multiplier": round(m, 3),
            "log_odds_per_point": round(float(per_point[j]), 5),
            "n_nonzero": int((X_raw[:, sig_cols.index(col)] != 0).sum()),
        })
    for col in dropped:
        weights.append({
            "signal": col,
            "current_multiplier": 1.0,
            "fitted_multiplier": None,
            "log_odds_per_point": None,
            "n_nonzero": int((X_raw[:, sig_cols.index(col)] != 0).sum()),
        })
    weights.sort(key=lambda w: (w["fitted_multiplier"] is None,
                                -abs(w["fitted_multiplier"] or 0.0)))

    # Rank quality on the evaluation window: current score vs refit linear score.
    refit_score = X_raw[:, active] @ (per_point / denom if denom > 0 else per_point)
    cur = pd.to_numeric(df["watchability_score"], errors="coerce").to_numpy(dtype=float)
    y_te, s_te, r_te = y_all[test_mask], cur[test_mask], refit_score[test_mask]
    ap_cur = average_precision(y_te, s_te)
    ap_ref = average_precision(y_te, r_te)

    n_pos_total = int(y_all.sum())
    block = {
        "evaluation_mode": mode,
        "n": int(len(df)),
        "n_train": int(train_mask.sum()),
        "n_test": int(test_mask.sum()),
        "n_pos_train": n_pos_train,
        "n_pos_total": n_pos_total,
        "l2": l2,
        "converged": bool(converged),
        "intercept": round(float(intercept), 4),
        "auc_pr_current_score": round(float(ap_cur), 4) if np.isfinite(ap_cur) else None,
        "auc_pr_refit_score": round(float(ap_ref), 4) if np.isfinite(ap_ref) else None,
        "weights": weights,
        "dropped_zero_variance": dropped,
    }
    if n_pos_total < 100:
        block["caveat"] = (
            f"Only {n_pos_total} positive label(s) for {service} — with n_pos < 100 "
            "these multipliers are HIGH-VARIANCE suggestions, not evidence. Collect "
            "more snapshot-days before trusting any reordering, and never apply a "
            "sign flip on a group that fired fewer than ~30 times.")
        warnings.append(block["caveat"])
    return block


def print_block(service: str, block: dict) -> None:
    print(f"\n── {service}  [{block['evaluation_mode']}]  "
          f"n={block['n']} (train {block['n_train']} / test {block['n_test']}), "
          f"positives={block['n_pos_total']}, l2={block['l2']}, "
          f"converged={block['converged']}")
    if block.get("n_by_source"):
        print(f"   rows by source: {block['n_by_source']}   "
              "(backfill rows carry known leakage — see warnings)")
    print(f"   AUC-PR on eval window — current score: {block['auc_pr_current_score']}   "
          f"refit linear score: {block['auc_pr_refit_score']}")
    print(f"   {'signal group':<28} {'current':>8} {'fitted':>8} {'perpoint':>10} {'fires':>6}")
    for w in block["weights"]:
        fm = f"{w['fitted_multiplier']:.3f}" if w["fitted_multiplier"] is not None else "  n/a"
        lo = f"{w['log_odds_per_point']:+.4f}" if w["log_odds_per_point"] is not None else "   n/a"
        print(f"   {w['signal']:<28} {w['current_multiplier']:>8.1f} {fm:>8} {lo:>10} "
              f"{w['n_nonzero']:>6}")
    if block.get("caveat"):
        print(f"   ⚠ {block['caveat']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--split-date", default=None,
                    help="YYYY-MM-DD; train=older, test=on/after. Omit → in-sample.")
    ap.add_argument("--l2", type=float, default=1.0, help="ridge strength (default 1.0)")
    ap.add_argument("--horizon-days", type=int, default=14)
    ap.add_argument("--service", choices=["radarr", "sonarr", "both"], default="both")
    ap.add_argument("--instance", default=None)
    ap.add_argument("--include-immature", action="store_true")
    ap.add_argument("--include-backfill", action="store_true",
                    help="also fit on source='backfill' snapshot rows "
                         "(ml_backfill_snapshots truncated-replay reconstructions "
                         "with KNOWN leakage; default: excluded)")
    ap.add_argument("--cache-base", default=None)
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args(argv)

    base = Path(args.cache_base) if args.cache_base else CacheKeyBuilder().base_dir
    services = ("radarr", "sonarr") if args.service == "both" else (args.service,)
    warnings: list[str] = []

    snaps = load_snapshots(base, services=services, instance=args.instance)
    if snaps.empty:
        print(f"No snapshots found under {base / 'ml' / 'snapshots'} — run the "
              "pipeline once with ml.snapshots.enabled (default ON) first.")
        return 1

    # ── provenance gate: backfilled rows are OPT-IN (--include-backfill) ──────
    src = snaps["source"] if "source" in snaps.columns \
        else pd.Series("prospective", index=snaps.index)
    rows_by_source = {str(k): int(v) for k, v in src.value_counts(dropna=False).items()}
    n_backfill = rows_by_source.get("backfill", 0)
    if args.include_backfill:
        if n_backfill:
            warnings.append(
                f"--include-backfill: rows by source {rows_by_source} — backfilled "
                "rows are TRUNCATED-REPLAY reconstructions with KNOWN leakage "
                "(credits_today, metadata_today, deletions_unknown); the fitted "
                "multipliers below are directional suggestions, not evidence.")
    elif n_backfill:
        snaps = snaps[src != "backfill"].reset_index(drop=True)
        warnings.append(
            f"{n_backfill} backfill snapshot row(s) EXCLUDED (default; pass "
            "--include-backfill to fit on them).")
        if snaps.empty:
            print(f"  ⚠ {warnings[-1]}")
            print("Only backfill snapshots exist — nothing prospective to fit.")
            return 1

    labeled = build_labels(snaps, base, horizon_days=args.horizon_days)
    if not args.include_immature:
        mature = labeled[labeled["label_mature"]]
        if mature.empty:
            warnings.append("No matured labels yet — using ALL rows; negatives are "
                            "provisional (the household may still watch them).")
        else:
            labeled = mature.reset_index(drop=True)

    report: dict = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "split_date": args.split_date,
        "horizon_days": args.horizon_days,
        "l2": args.l2,
        "instance": args.instance,
        "include_backfill": bool(args.include_backfill),
        "rows_by_source": rows_by_source,
        "applied": False,   # this tool NEVER applies anything
        "how_to_read": ("fitted_multiplier is relative per-point evidence, "
                        "normalized so the strongest group = 1.0 (today every "
                        "group is implicitly 1.0). Negative = correlates with "
                        "not-watching. n/a = the group never fired in training."),
        "warnings": warnings,
        "services": {},
    }
    print("=" * 78)
    print("WEIGHT REFIT — suggested per-signal-group multipliers (NOT applied)")
    print("=" * 78)
    for svc in services:
        sdf = labeled[labeled["service"] == svc].reset_index(drop=True)
        if sdf.empty:
            continue
        block = refit_service(sdf, args.split_date, args.l2, warnings, svc)
        if block:
            if args.include_backfill and "source" in sdf.columns:
                block["n_by_source"] = {
                    str(k): int(v)
                    for k, v in sdf["source"].value_counts(dropna=False).items()}
            report["services"][svc] = block
            print_block(svc, block)
    for w in warnings:
        print(f"  ⚠ {w}")
    if not report["services"]:
        print("No fittable data (no sig_* columns or no positives).")
        return 1

    if not args.no_write:
        out_dir = base / "ml" / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"suggested_weights_{datetime.now(tz=timezone.utc):%Y-%m-%d}.json"
        out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nSuggestions written: {out_path}  (informational only — never auto-applied)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
