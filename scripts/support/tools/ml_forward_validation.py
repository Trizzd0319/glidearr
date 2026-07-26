"""
ml_forward_validation.py — how well does the EXISTING watchability score predict
what the household actually watches next? (ML Stage 2 — offline, read-only)
================================================================================
Standalone CLI (never imported by main.py). Loads the Stage-1 snapshot Parquets
(<cache>/ml/snapshots/{service}/*.parquet), joins Tautulli ground truth via
labels/labeling.build_labels, temporally splits on --split-date, and evaluates
the hand-weighted 0-100 score as a ranker/classifier on the TEST window:

  * AUC-PR (average precision — numpy implementation, sklearn absent)
  * Brier score of the min-max-scaled score treated as P(watch)
  * calibration table (10 bins: mean scaled score vs empirical watch rate)
  * per-signal-group univariate AUC-PR (sig_* columns) — weak signals visible

Degenerate data (single snapshot batch, empty test window, no matured labels)
degrades to a clearly-labeled IN-SAMPLE evaluation with a warning rather than
failing — so this runs cleanly on day one.

Usage
-----
    python scripts/support/tools/ml_forward_validation.py
    python scripts/support/tools/ml_forward_validation.py --split-date 2026-08-01
    python scripts/support/tools/ml_forward_validation.py --service radarr --horizon-days 7
    python scripts/support/tools/ml_forward_validation.py --instance standard --include-immature
    python scripts/support/tools/ml_forward_validation.py --no-write   # print only

Output: printed report + JSON at <cache>/ml/reports/forward_validation_{date}.json

No network, no *arr writes — reads local Parquet/JSON caches only.
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
    brier_score,
    calibration_table,
    minmax_scale,
)
from scripts.managers.machine_learning.labels.labeling import build_labels    # noqa: E402
from scripts.managers.machine_learning.labels.snapshots import load_snapshots # noqa: E402


def evaluate_window(df: pd.DataFrame, bins: int = 10) -> dict:
    """Score-vs-label metrics for one evaluation window (pure)."""
    y = df["watched_within_h"].astype(float).to_numpy()
    s = pd.to_numeric(df["watchability_score"], errors="coerce").to_numpy(dtype=float)
    p = minmax_scale(s)
    n_pos = int(y.sum())
    out = {
        "n": int(len(df)),
        "n_pos": n_pos,
        "base_rate": round(float(y.mean()), 4) if len(df) else None,
        "auc_pr": None,
        "auc_pr_lift_over_base": None,
        "brier_minmax_score": None,
        "calibration": calibration_table(y, p, bins=bins),
        "recommended_not_watched": int(df.get(
            "recommended_not_watched", pd.Series(dtype=bool)).fillna(False).sum()),
    }
    ap = average_precision(y, s)
    if np.isfinite(ap):
        out["auc_pr"] = round(float(ap), 4)
        if y.mean() > 0:
            out["auc_pr_lift_over_base"] = round(float(ap / y.mean()), 2)
    b = brier_score(y, p)
    if np.isfinite(b):
        out["brier_minmax_score"] = round(float(b), 4)
    return out


def per_signal_auc_pr(df: pd.DataFrame) -> list[dict]:
    """Univariate AUC-PR per sig_* column, sorted best-first. A signal that is
    zero everywhere (never fires) reports auc_pr=None with its fire count."""
    y = df["watched_within_h"].astype(float).to_numpy()
    rows: list[dict] = []
    for col in sorted(c for c in df.columns if c.startswith("sig_")):
        v = pd.to_numeric(df[col], errors="coerce").to_numpy(dtype=float)
        fires = int(np.sum(np.nan_to_num(v) != 0.0))
        ap = average_precision(y, np.nan_to_num(v, nan=0.0))
        rows.append({
            "signal": col,
            "auc_pr": round(float(ap), 4) if np.isfinite(ap) else None,
            "n_nonzero": fires,
        })
    rows.sort(key=lambda r: (r["auc_pr"] is None, -(r["auc_pr"] or 0.0)))
    return rows


def print_report(report: dict) -> None:
    mode = report["evaluation_mode"]
    print("=" * 78)
    print(f"FORWARD VALIDATION — existing watchability score   [{mode}]")
    print("=" * 78)
    for w in report.get("warnings", []):
        print(f"  ⚠ {w}")
    for svc, block in report["services"].items():
        m = block["metrics"]
        print(f"\n── {svc}  (n={m['n']}, positives={m['n_pos']}, "
              f"base rate={m['base_rate']})")
        if m.get("n_by_source"):
            print(f"   rows by source: {m['n_by_source']}   "
                  "(backfill rows carry known leakage — see warnings)")
        print(f"   AUC-PR: {m['auc_pr']}   (lift over random: "
              f"{m['auc_pr_lift_over_base']}x)")
        print(f"   Brier (min-max score as P): {m['brier_minmax_score']}")
        print(f"   recommended_not_watched rows: {m['recommended_not_watched']}")
        print("   calibration (scaled score bin → watch rate):")
        for b in m["calibration"]:
            if not b["n"]:
                continue
            print(f"     [{b['lo']:.1f},{b['hi']:.1f})  n={b['n']:<5} "
                  f"mean_pred={b['mean_pred']}  watch_rate={b['watch_rate']}")
        print("   per-signal univariate AUC-PR (weak signals at the bottom):")
        for srow in block["per_signal_auc_pr"]:
            ap = srow["auc_pr"] if srow["auc_pr"] is not None else "  n/a"
            print(f"     {srow['signal']:<28} AP={ap}  fires={srow['n_nonzero']}")
    print()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--split-date", default=None,
                    help="YYYY-MM-DD; train=older, test=on/after. Omit → in-sample.")
    ap.add_argument("--horizon-days", type=int, default=14)
    ap.add_argument("--service", choices=["radarr", "sonarr", "both"], default="both")
    ap.add_argument("--instance", default=None)
    ap.add_argument("--include-immature", action="store_true",
                    help="also evaluate rows whose horizon has not fully elapsed "
                         "(their negatives are provisional)")
    ap.add_argument("--include-backfill", action="store_true",
                    help="also evaluate source='backfill' snapshot rows "
                         "(ml_backfill_snapshots truncated-replay reconstructions "
                         "with KNOWN leakage; default: excluded)")
    ap.add_argument("--bins", type=int, default=10)
    ap.add_argument("--cache-base", default=None,
                    help="override the global cache base dir (tests)")
    ap.add_argument("--no-write", action="store_true", help="print only, no JSON")
    args = ap.parse_args(argv)

    base = Path(args.cache_base) if args.cache_base else CacheKeyBuilder().base_dir
    services = ("radarr", "sonarr") if args.service == "both" else (args.service,)
    warnings: list[str] = []

    snaps = load_snapshots(base, services=services, instance=args.instance)
    if snaps.empty:
        print("No snapshots found under "
              f"{base / 'ml' / 'snapshots'} — run the pipeline once with "
              "ml.snapshots.enabled (default ON) to collect the first batch.")
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
                "(credits_today, metadata_today, deletions_unknown); every metric "
                "mixing them is directional, not evidence.")
    elif n_backfill:
        snaps = snaps[src != "backfill"].reset_index(drop=True)
        warnings.append(
            f"{n_backfill} backfill snapshot row(s) EXCLUDED (default; pass "
            "--include-backfill to evaluate them).")
        if snaps.empty:
            print(f"  ⚠ {warnings[-1]}")
            print("Only backfill snapshots exist — nothing prospective to evaluate.")
            return 1

    labeled = build_labels(snaps, base, horizon_days=args.horizon_days)
    n_immature = int((~labeled["label_mature"]).sum())
    if not args.include_immature:
        eval_df = labeled[labeled["label_mature"]].reset_index(drop=True)
        if eval_df.empty:
            warnings.append(
                f"No snapshot row is older than the {args.horizon_days}-day horizon yet "
                f"({n_immature} immature rows) — evaluating ALL rows in-sample; "
                "negatives are PROVISIONAL (the household may still watch them).")
            eval_df = labeled.reset_index(drop=True)
    else:
        eval_df = labeled.reset_index(drop=True)
        if n_immature:
            warnings.append(f"{n_immature} immature row(s) included (--include-immature) "
                            "— their negatives are provisional.")

    # ── temporal split ────────────────────────────────────────────────────────
    mode = "forward_holdout"
    test_df = eval_df
    if args.split_date:
        test_df = eval_df[eval_df["snapshot_date"] >= args.split_date].reset_index(drop=True)
        train_n = int((eval_df["snapshot_date"] < args.split_date).sum())
        if test_df.empty or train_n == 0:
            warnings.append(
                f"Degenerate split at {args.split_date} (train={train_n}, "
                f"test={len(test_df)}) — falling back to IN-SAMPLE evaluation "
                "over all rows.")
            mode = "in_sample_degenerate"
            test_df = eval_df
    else:
        n_days = eval_df["snapshot_date"].nunique()
        warnings.append(
            f"No --split-date given ({n_days} distinct snapshot day(s) on disk) — "
            "IN-SAMPLE evaluation over all rows. With more history, pass "
            "--split-date to hold out a forward window.")
        mode = "in_sample"

    report: dict = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "evaluation_mode": mode,
        "split_date": args.split_date,
        "horizon_days": args.horizon_days,
        "instance": args.instance,
        "include_backfill": bool(args.include_backfill),
        "rows_by_source": rows_by_source,
        "snapshot_days": sorted(eval_df["snapshot_date"].astype(str).unique().tolist()),
        "warnings": warnings,
        "services": {},
    }
    for svc in services:
        sdf = test_df[test_df["service"] == svc].reset_index(drop=True)
        if sdf.empty:
            continue
        metrics = evaluate_window(sdf, bins=args.bins)
        if args.include_backfill and "source" in sdf.columns:
            metrics["n_by_source"] = {
                str(k): int(v) for k, v in sdf["source"].value_counts(dropna=False).items()}
        if metrics["n_pos"] < 100:
            warnings.append(
                f"{svc}: only {metrics['n_pos']} positive label(s) — every metric "
                "here is high-variance; treat as directional, not conclusive.")
        report["services"][svc] = {
            "metrics": metrics,
            "per_signal_auc_pr": per_signal_auc_pr(sdf),
        }

    print_report(report)

    if not args.no_write:
        out_dir = base / "ml" / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"forward_validation_{datetime.now(tz=timezone.utc):%Y-%m-%d}.json"
        out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Report written: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
