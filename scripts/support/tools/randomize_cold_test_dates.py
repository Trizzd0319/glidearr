"""
randomize_cold_test_dates.py — stagger the cold-TV test population (GLD-ACQ-24).
================================================================================
Test-data tool. Operates ONLY on ``row_origin == 'cold_scan'`` parquet rows —
never watched rows, never pilots, never stubs, never hot series. Run it AFTER at
least one ingest run has populated cold rows (❄️ [ColdTV] line).

WHY TWO MODES — the mechanical truth this script encodes:
  * The ownership-age gate (``min_owned_days``) reads ``dateAdded`` from the LIVE
    Sonarr episodefile API at ingest time (no on-disk cache exists for it), so
    ownership dates CANNOT be faked from our side. ``--mode dates`` therefore
    randomizes the parquet's recorded ``date_added`` only — cosmetic variety in
    grids, zero lifecycle effect. It exists to honor the literal request and to
    make test data look organic.
  * What actually staggers the lifecycle is ``available_until``: the mark pass
    fires when it expires. ``--mode windows`` (DEFAULT) re-rolls each cold row's
    window so a chosen fraction is already expired (marks next run) and the rest
    expire randomly across the coming days — a rolling population that lets you
    watch ingest → mark → pool → (apply) repeatedly instead of one big bang.

``--unmark`` resets ``marked_for_deletion`` on cold rows first, so a finished
test can be re-rolled and replayed ("window of options to test again").

SAFE BY CONSTRUCTION: dry-run by default (prints the distribution, writes
nothing); ``--apply`` writes after a timestamped ``.bak`` of each parquet;
non-cold rows are asserted byte-identical after mutation.

Examples (from repo root):
    python scripts\\support\\tools\\randomize_cold_test_dates.py                 # preview
    python scripts\\support\\tools\\randomize_cold_test_dates.py --apply         # stagger windows
    python scripts\\support\\tools\\randomize_cold_test_dates.py --unmark --apply  # reset + re-roll
    python scripts\\support\\tools\\randomize_cold_test_dates.py --mode dates --past-days 90 --apply
"""
from __future__ import annotations

import argparse
import random
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

CACHE_SONARR = Path(__file__).resolve().parents[1] / "cache" / "sonarr"


def randomize(df: "pd.DataFrame", *, mode: str, past_days: int, future_days: int,
              expired_frac: float, unmark: bool, rng: "random.Random",
              now: "datetime | None" = None) -> tuple["pd.DataFrame", dict]:
    """Pure mutation core (testable). Returns ``(df, stats)``. Touches ONLY
    ``row_origin == 'cold_scan'`` rows; everything else is asserted unchanged."""
    now = now or datetime.now(tz=timezone.utc)
    stats = {"cold": 0, "unmarked": 0, "expired": 0, "future": 0, "dated": 0}
    if df is None or df.empty or "row_origin" not in df.columns:
        return df, stats

    cold_idx = df.index[df["row_origin"] == "cold_scan"]
    stats["cold"] = len(cold_idx)
    if not len(cold_idx):
        return df, stats

    before_other = df.loc[df["row_origin"] != "cold_scan"].copy()

    for i in cold_idx:
        if unmark and bool(df.at[i, "marked_for_deletion"]):
            df.at[i, "marked_for_deletion"] = False
            stats["unmarked"] += 1
        if mode == "dates":
            dt = now - timedelta(days=rng.uniform(0, past_days))
            df.at[i, "date_added"] = dt.isoformat()
            stats["dated"] += 1
        else:  # windows
            if rng.random() < expired_frac:
                dt = now - timedelta(days=rng.uniform(0.01, max(past_days, 1)))
                stats["expired"] += 1
            else:
                dt = now + timedelta(days=rng.uniform(0.25, max(future_days, 1)))
                stats["future"] += 1
            df.at[i, "available_until"] = dt.isoformat()

    after_other = df.loc[df["row_origin"] != "cold_scan"]
    assert before_other.equals(after_other), "NON-COLD ROWS CHANGED — aborting"
    return df, stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--mode", choices=("windows", "dates"), default="windows")
    ap.add_argument("--past-days", type=int, default=90,
                    help="dates mode: uniform ownership-date window; windows mode: how far back expired windows land")
    ap.add_argument("--future-days", type=int, default=21,
                    help="windows mode: unexpired windows spread across the next N days")
    ap.add_argument("--expired-frac", type=float, default=0.66,
                    help="windows mode: fraction already expired (marks next run)")
    ap.add_argument("--unmark", action="store_true",
                    help="reset marked_for_deletion on cold rows first (replay the cycle)")
    ap.add_argument("--apply", action="store_true", help="write changes (default: preview only)")
    ap.add_argument("--seed", type=int, default=None, help="reproducible randomness")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    parquets = sorted(CACHE_SONARR.glob("*/episode_files.parquet"))
    if not parquets:
        print(f"no parquets under {CACHE_SONARR}")
        return 1

    for pq in parquets:
        inst = pq.parent.name
        df = pd.read_parquet(pq)
        df, stats = randomize(df, mode=args.mode, past_days=args.past_days,
                              future_days=args.future_days,
                              expired_frac=args.expired_frac,
                              unmark=args.unmark, rng=rng)
        tag = "APPLY" if args.apply else "PREVIEW"
        if args.mode == "windows":
            detail = (f"{stats['expired']} window(s) expired-now, "
                      f"{stats['future']} spread over next {args.future_days}d")
        else:
            detail = f"{stats['dated']} date_added randomized over past {args.past_days}d"
        _um = f"; {stats['unmarked']} unmarked for replay" if args.unmark else ""
        print(f"[{tag}] '{inst}': {stats['cold']} cold row(s); {detail}{_um}")
        if args.apply and stats["cold"]:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = pq.with_name(f"episode_files.parquet.bak-{stamp}")
            shutil.copy2(pq, backup)
            df.to_parquet(pq, index=False)
            print(f"        written; backup: {backup.name}")
    if not args.apply:
        print("(preview only — re-run with --apply to write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
