"""Replicate _recycle_to_fund_acquisition's eligibility filter and report WHERE it drops.

Run from the repo root:
    python diag_recycle.py

Prints, for the series that actually have pending next-up episodes, how many owned
episodes survive each successive filter. The first stage that goes to zero is the answer.
"""
import sys
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, ".")

INST = "standard"
PARQUET = f"scripts/support/cache/sonarr/{INST}/episode_files.parquet"

df = pd.read_parquet(PARQUET)
print(f"rows: {len(df)}")

# The pending set the acquire pass builds.
pending = (df["next_episode"].fillna(False).astype(bool) & df["episode_file_id"].isna())
print(f"pending next-up (next_episode & no file): {int(pending.sum())}")
if not pending.any():
    print("\n>>> NOTHING PENDING. The recycle is never reached because there is nothing to fund.")
    raise SystemExit

pend_sids = sorted({int(s) for s in df.loc[pending, "series_id"].dropna()})
print(f"series with pending episodes: {len(pend_sids)} -> {pend_sids[:12]}")

# The whole-file guard set, exactly as the manager builds it.
try:
    from scripts.managers.machine_learning.classification.guards import (
        build_pilot_file_ids, build_protected_file_ids,
    )
    now = datetime.now(tz=timezone.utc)
    pilots = build_pilot_file_ids(df)
    protected = build_protected_file_ids(df, now, pilots, recent_air_days=30)
    print(f"pilot file ids: {len(pilots)}   protected file ids: {len(protected)}")
except Exception as e:  # noqa: BLE001
    print(f"!! could not build guard set: {e}")
    protected = set()

print("\nper-series funnel (only series with pending episodes):")
print(f"{'sid':>7} {'title':<34} {'owned':>6} {'watched':>8} {'unguard':>8} {'dated':>6} {'sized':>6}")
print("-" * 82)

tot = dict(owned=0, watched=0, unguarded=0, dated=0, sized=0)
for sid in pend_sids:
    rows = df[pd.to_numeric(df["series_id"], errors="coerce") == sid]
    title = str(rows["series_title"].dropna().iloc[0])[:32] if rows["series_title"].notna().any() else "?"

    owned = rows[rows["episode_file_id"].notna()]
    watched = owned[owned["is_watched"].fillna(False).astype(bool)]
    unguarded = watched[~watched["episode_file_id"].astype("Int64").isin(list(protected))]

    def _anchor(r):
        h = r.get("household_last_watched_at")
        return h if (pd.notna(h) and h) else r.get("last_watched_at")

    dated = unguarded[unguarded.apply(lambda r: pd.notna(_anchor(r)) and bool(_anchor(r)), axis=1)] \
        if len(unguarded) else unguarded
    sized = dated[dated["size_bytes"].notna()] if len(dated) else dated

    tot["owned"] += len(owned); tot["watched"] += len(watched)
    tot["unguarded"] += len(unguarded); tot["dated"] += len(dated); tot["sized"] += len(sized)
    print(f"{sid:>7} {title:<34} {len(owned):>6} {len(watched):>8} "
          f"{len(unguarded):>8} {len(dated):>6} {len(sized):>6}")

print("-" * 82)
print(f"{'TOTAL':>7} {'':<34} {tot['owned']:>6} {tot['watched']:>8} "
      f"{tot['unguarded']:>8} {tot['dated']:>6} {tot['sized']:>6}")

print("\nread the funnel left to right; the first column that collapses is the cause:")
print("  owned->watched   : nothing watched in these series")
print("  watched->unguard : the WHOLE-FILE GUARD SET is eating them (pilot/keep/recent-air/")
print("                     household/retention/watchlist) -- GLD-ACQ-18 territory")
print("  unguard->dated   : no watch timestamp on any anchor column")
print("  dated->sized     : size_bytes missing")
print("\nNOTE: the rewatch_buffer (default 2) then holds back the 2 most recent of whatever")
print("survives, so a series needs >2 eligible episodes to fund anything at all.")