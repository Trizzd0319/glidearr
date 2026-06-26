"""
force_pilot_research.py — clear pilot search timestamps so stub pilots are due again.
================================================================================
The pilot search re-probes a stub at most once every 24h (the interval guard reads
``pilot_last_searched_at`` on each stub row). To re-search SOONER — e.g. to validate
the pilot-search daemon end-to-end without waiting out the interval — this clears that
timestamp for stub pilots (``is_pilot`` & no ``episode_file_id``) in the instance's
``episode_files.parquet``. The NEXT run then sees them as due and (when the batch
exceeds ``daemons.pilot_search.threshold``) spills them to the daemon.

SAFE: refuses to run while a main.py run is active (the run holds the dataframe in
memory and its ``save()`` would clobber the clear); always backs up the parquet first.

    python scripts/support/tools/force_pilot_research.py                 # first 200 stubs (default)
    python scripts/support/tools/force_pilot_research.py --limit 50      # first 50 stubs
    python scripts/support/tools/force_pilot_research.py --all           # every stub (heavy!)
    python scripts/support/tools/force_pilot_research.py --instance standard
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import pandas as pd

# repo root: scripts/support/tools/force_pilot_research.py → parents[3]
_REPO_ROOT = Path(__file__).resolve().parents[3]
_CACHE = _REPO_ROOT / "scripts" / "support" / "cache"
_SENTINEL = _CACHE / "trakt" / "main_run.active"


def _run_active() -> tuple[bool, str]:
    """(active, detail). A fresh main_run.active sentinel whose pid is alive ⇒ a run is in
    progress. Absent sentinel ⇒ no run. If we can't determine, treat as ACTIVE (fail safe)."""
    if not _SENTINEL.exists():
        return False, "no run sentinel"
    try:
        d = json.loads(_SENTINEL.read_text() or "{}")
        pid = int(d.get("pid", 0))
        age = time.time() - float(d.get("ts", 0))
    except Exception:
        return True, "sentinel unreadable — refusing (fail safe)"
    if not pid:
        return False, "sentinel has no pid"
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # Windows
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True, f"run pid {pid} alive (age {age/60:.1f} min)"
        return False, f"sentinel pid {pid} is dead (stale)"
    except Exception:
        try:
            import os
            os.kill(pid, 0)                     # POSIX
            return True, f"run pid {pid} alive (age {age/60:.1f} min)"
        except ProcessLookupError:
            return False, f"sentinel pid {pid} is dead (stale)"
        except Exception:
            return True, "could not probe run pid — refusing (fail safe)"


def main() -> int:
    ap = argparse.ArgumentParser(description="Force pilot stubs due by clearing pilot_last_searched_at")
    ap.add_argument("--instance", default="standard", help="Sonarr instance name (default: standard)")
    ap.add_argument("--limit", type=int, default=200, help="Clear at most this many stub pilots (default: 200)")
    ap.add_argument("--all", action="store_true", help="Clear EVERY stub pilot (overrides --limit; heavy)")
    args = ap.parse_args()

    active, detail = _run_active()
    if active:
        print(f"ABORT: {detail}. Let the run finish, then re-run this.")
        return 1

    pq = _CACHE / "sonarr" / args.instance / "episode_files.parquet"
    if not pq.exists():
        print(f"ABORT: parquet not found: {pq}")
        return 1

    bak = pq.with_suffix(".parquet.prebak")
    shutil.copy2(pq, bak)
    df = pd.read_parquet(pq)
    if "pilot_last_searched_at" not in df.columns:
        print("ABORT: no pilot_last_searched_at column — nothing to clear.")
        return 1

    stub = df["is_pilot"].fillna(False).astype(bool) & df["episode_file_id"].isna()
    stub_idx = list(df.index[stub])
    if not args.all:
        stub_idx = stub_idx[: max(0, args.limit)]
    if not stub_idx:
        print("Nothing to clear (no stub pilots).")
        return 0

    df["pilot_last_searched_at"] = df["pilot_last_searched_at"].astype(object)
    df.loc[stub_idx, "pilot_last_searched_at"] = None
    df.to_parquet(pq, index=False)

    df2 = pd.read_parquet(pq)
    stub2 = df2["is_pilot"].fillna(False).astype(bool) & df2["episode_file_id"].isna()
    due = int(df2.loc[stub2, "pilot_last_searched_at"].isna().sum())
    print(f"CLEARED {len(stub_idx)} stub(s) for '{args.instance}'. "
          f"Stubs now due (last_searched empty): {due} / {int(stub2.sum())} total.")
    print(f"Backup: {bak}")
    print("Next run will spill the due batch to the pilot-search daemon "
          "(check: python scripts/support/daemons/pilot_search_daemon.py --status).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
