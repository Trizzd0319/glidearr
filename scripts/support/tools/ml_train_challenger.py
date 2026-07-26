"""
ml_train_challenger.py — train the shadow GBT watch-probability challenger.
(ML Stage 4 — offline; the trained model is OBSERVE-ONLY at runtime)
================================================================================
Standalone CLI (never imported by main.py). Loads the Stage-1 snapshots, labels
them against Tautulli ground truth, and trains a small LightGBM classifier per
service with a temporal split + early stopping, then calibrates probabilities
with pure-numpy isotonic regression on the validation window. Artifacts land in
<cache>/ml/models/gbt_challenger_{service}.{txt,calib.json}.

The RUNTIME shadow hook (challenger/gbt_shadow.attach_challenger_p) only fires
when config ``scoring.ml_challenger.enabled`` is true (DEFAULT FALSE) AND these
model files exist — and even then it only logs score-vs-P divergence and stamps
``challenger_p`` into the snapshot rows. It never feeds a decision.

Requires lightgbm (optional dependency): exits cleanly with a message when absent.

Usage
-----
    python scripts/support/tools/ml_train_challenger.py
    python scripts/support/tools/ml_train_challenger.py --service radarr --split-date 2026-08-01
    python scripts/support/tools/ml_train_challenger.py --horizon-days 7 --rounds 200
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from scripts.managers.factories.cache.key_builder import CacheKeyBuilder       # noqa: E402
from scripts.managers.machine_learning.challenger.gbt_shadow import (          # noqa: E402
    HAS_LIGHTGBM,
    train_challenger,
)
from scripts.managers.machine_learning.labels.labeling import build_labels     # noqa: E402
from scripts.managers.machine_learning.labels.snapshots import load_snapshots  # noqa: E402


class _PrintLogger:
    def log_info(self, m): print(m)
    def log_warning(self, m): print(f"WARNING: {m}")
    def log_debug(self, m): print(f"debug: {m}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--split-date", default=None,
                    help="YYYY-MM-DD; train < split <= valid. Omit → last ~20%% of "
                         "snapshot days become validation.")
    ap.add_argument("--horizon-days", type=int, default=14)
    ap.add_argument("--service", choices=["radarr", "sonarr", "both"], default="both")
    ap.add_argument("--instance", default=None)
    ap.add_argument("--include-immature", action="store_true",
                    help="train on rows whose horizon has not elapsed (noisy negatives)")
    ap.add_argument("--include-backfill", action="store_true",
                    help="also train on source='backfill' snapshot rows "
                         "(ml_backfill_snapshots truncated-replay reconstructions "
                         "with KNOWN leakage; default: excluded)")
    ap.add_argument("--rounds", type=int, default=400, help="max boosting rounds")
    ap.add_argument("--early-stopping", type=int, default=30)
    ap.add_argument("--cache-base", default=None)
    args = ap.parse_args(argv)

    if not HAS_LIGHTGBM:
        print("lightgbm is not installed — the shadow challenger cannot be trained "
              "here. Install it (pip install lightgbm) or skip Stage 4; everything "
              "else works without it.")
        return 1

    base = Path(args.cache_base) if args.cache_base else CacheKeyBuilder().base_dir
    services = ("radarr", "sonarr") if args.service == "both" else (args.service,)
    log = _PrintLogger()

    snaps = load_snapshots(base, services=services, instance=args.instance)
    if snaps.empty:
        print(f"No snapshots found under {base / 'ml' / 'snapshots'} — run the "
              "pipeline once with ml.snapshots.enabled (default ON) first.")
        return 1

    # ── provenance gate: backfilled rows are OPT-IN (--include-backfill) ──────
    if "source" in snaps.columns:
        rows_by_source = {str(k): int(v)
                          for k, v in snaps["source"].value_counts(dropna=False).items()}
    else:
        rows_by_source = {"prospective": int(len(snaps))}
    n_backfill = rows_by_source.get("backfill", 0)
    if args.include_backfill:
        if n_backfill:
            print(f"WARNING: --include-backfill — rows by source {rows_by_source}; "
                  "backfilled rows are TRUNCATED-REPLAY reconstructions with KNOWN "
                  "leakage (credits_today, metadata_today, deletions_unknown) — the "
                  "trained shadow model inherits that leakage.")
    elif n_backfill:
        snaps = snaps[snaps["source"] != "backfill"].reset_index(drop=True)
        print(f"NOTE: {n_backfill} backfill snapshot row(s) EXCLUDED "
              "(default; pass --include-backfill to train on them).")
        if snaps.empty:
            print("Only backfill snapshots exist — nothing prospective to train on.")
            return 1

    labeled = build_labels(snaps, base, horizon_days=args.horizon_days)
    if not args.include_immature:
        mature = labeled[labeled["label_mature"]]
        if mature.empty:
            print("WARNING: no matured labels yet — training on ALL rows; negatives "
                  "are provisional. Prefer waiting a horizon before trusting this model.")
        else:
            labeled = mature.reset_index(drop=True)

    trained = 0
    for svc in services:
        print(f"\n── training challenger for {svc} "
              f"(n={int((labeled['service'] == svc).sum())}) ──")
        result = train_challenger(
            labeled, svc, base, split_date=args.split_date,
            num_boost_round=args.rounds, early_stopping_rounds=args.early_stopping,
            logger=log)
        if not result:
            print(f"   {svc}: not trained (see warnings above).")
            continue
        trained += 1
        m = result["metrics"]
        print(f"   model:  {result['model_path']}")
        print(f"   calib:  {result['calib_path']}")
        print(f"   n_train={result['n_train']}  n_valid={result['n_valid']}  "
              f"best_iteration={m['best_iteration']}")
        print(f"   valid AUC-PR={m['valid_auc_pr']}  (base rate {m['valid_base_rate']})")
        print(f"   valid Brier raw={m['valid_brier_raw']} → calibrated={m['valid_brier_calibrated']}")
        if m.get("degenerate_split"):
            print("   ⚠ degenerate split (too few snapshot days) — validation is "
                  "random, not temporal; metrics are optimistic.")
    if trained:
        print("\nShadow challenger trained. To see divergence logging on the next run, "
              "set config scoring.ml_challenger.enabled = true (observe-only — it "
              "never feeds a decision).")
    return 0 if trained else 1


if __name__ == "__main__":
    raise SystemExit(main())
