"""Name WHICH guard protects each watched episode in the series that have pending next-ups.

    python diag_guards.py

Prints one row per watched+owned episode with a tick under every guard that catches it.
The column that is ticked everywhere is the one to fix.
"""
import sys
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, ".")

INST = "standard"
df = pd.read_parquet(f"scripts/support/cache/sonarr/{INST}/episode_files.parquet")
now = datetime.now(tz=timezone.utc)

from scripts.managers.machine_learning.classification.guards import build_pilot_file_ids
from scripts.managers.machine_learning.space.downgrade_planner import UNIVERSE_PROTECT_MIN

pilots = build_pilot_file_ids(df)

pending = (df["next_episode"].fillna(False).astype(bool) & df["episode_file_id"].isna())
sids = sorted({int(s) for s in df.loc[pending, "series_id"].dropna()})

RECENT_AIR_DAYS = 30
air_all = pd.to_datetime(df["air_date_utc"], utc=True, errors="coerce")
days_since_all = (now - air_all).dt.days

hdr = f"{'series':<26} {'ep':<8} {'pilot':>5} {'keep':>5} {'air':>5} {'hhold':>5} {'reten':>5} {'wlist':>5} {'univ':>5}"
print(hdr)
print("-" * len(hdr))

counts = dict(pilot=0, keep=0, air=0, hhold=0, reten=0, wlist=0, univ=0, none=0, total=0)

for sid in sids:
    rows = df[pd.to_numeric(df["series_id"], errors="coerce") == sid]
    title = str(rows["series_title"].dropna().iloc[0])[:24] if rows["series_title"].notna().any() else "?"
    owned_watched = rows[rows["episode_file_id"].notna()
                         & rows["is_watched"].fillna(False).astype(bool)]
    for i in owned_watched.index:
        fid = int(df.at[i, "episode_file_id"])
        sn = df.at[i, "season_number"]; en = df.at[i, "episode_number"]
        ep = f"S{int(sn):02d}E{int(en):02d}" if pd.notna(sn) and pd.notna(en) else "?"

        g = {}
        g["pilot"] = fid in pilots
        kp = df.at[i, "keep_policy"] if "keep_policy" in df.columns else None
        g["keep"] = str(kp) in ("keep_series", "keep_season")
        _d = days_since_all.at[i]
        g["air"] = bool(pd.notna(_d) and _d < RECENT_AIR_DAYS)
        _a = df.at[i, "all_household_watched"] if "all_household_watched" in df.columns else None
        g["hhold"] = bool(pd.notna(_a) and not bool(_a))
        _r = df.at[i, "retention_hold"] if "retention_hold" in df.columns else None
        g["reten"] = bool(pd.notna(_r) and bool(_r))
        _w = df.at[i, "watchlist_hold"] if "watchlist_hold" in df.columns else None
        g["wlist"] = bool(pd.notna(_w) and bool(_w))
        _u = pd.to_numeric(pd.Series([df.at[i, "universe_credit"]]), errors="coerce").iloc[0] \
            if "universe_credit" in df.columns else None
        g["univ"] = bool(pd.notna(_u) and _u >= UNIVERSE_PROTECT_MIN)

        counts["total"] += 1
        for k, v in g.items():
            if v:
                counts[k] += 1
        if not any(g.values()):
            counts["none"] += 1

        cells = "".join(f"{('YES' if g[k] else '-'):>6}"
                        for k in ("pilot", "keep", "air", "hhold", "reten", "wlist", "univ"))
        print(f"{title:<26} {ep:<8}{cells}")

print("-" * len(hdr))
print(f"\n{counts['total']} watched+owned episode(s) across {len(sids)} series")
for k in ("pilot", "keep", "air", "hhold", "reten", "wlist", "univ"):
    print(f"  caught by {k:<6}: {counts[k]}")
print(f"  caught by NOTHING : {counts['none']}   <- these are the recyclable ones")
print("\nThe guard ticked on (nearly) every row is the one blocking the recycle.")