"""
ml_survival_report.py — household hazard curves: when does the next watch come?
(ML Stage 5a — offline, read-only; the survival API is never wired into the run)
================================================================================
Standalone CLI (never imported by main.py). Reads Tautulli history
(tautulli/history/all.json — fields: date/media_type/rating_key/
grandparent_title/percent_complete, movies joined to tmdb via
plex/movies/owned_inventory rating_key), fits the pooled discrete-time hazard
model (likelihood/survival.py) and prints:

  * the HOUSEHOLD hazard curve — P(next watch in bucket | none yet) per
    day-bucket, with at-risk counts so thin tails are visibly thin
  * residual-probability examples: P(watch within H | gap so far = g)
  * the most-rewatched entities' own blended curves (>=3 events)

Grouping for the middle pool: movies inherit their TMDB collection_name from
movie_files.parquet when present (franchise pool); episodes pool by series.

Usage
-----
    python scripts/support/tools/ml_survival_report.py
    python scripts/support/tools/ml_survival_report.py --bucket-days 7 --max-days 364
    python scripts/support/tools/ml_survival_report.py --horizon 14 --top 8
    python scripts/support/tools/ml_survival_report.py --min-pct 50
"""
from __future__ import annotations

import argparse
import glob
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

from scripts.managers.factories.cache.key_builder import CacheKeyBuilder       # noqa: E402
from scripts.managers.machine_learning.labels.labeling import (                # noqa: E402
    load_watch_events,
    normalize_title,
)
from scripts.managers.machine_learning.likelihood.survival import (            # noqa: E402
    fit_survival_model,
)


def build_events_by_entity(events: pd.DataFrame, min_pct: float) -> "tuple[dict, dict]":
    """{entity: [event ts]} + display names. Movies keyed ('movie', tmdb);
    episodes pooled per series ('show', normalized title)."""
    events_by_entity: dict = {}
    names: dict = {}
    for rec in events.to_dict("records"):
        if float(rec.get("percent_complete") or 0) < min_pct:
            continue
        if rec["media_type"] == "movie":
            tmdb = rec.get("tmdb_id")
            if tmdb is None or (isinstance(tmdb, float) and pd.isna(tmdb)):
                continue
            key = ("movie", int(tmdb))
        elif rec["media_type"] == "episode":
            t = rec.get("series_title_norm") or ""
            if not t:
                continue
            key = ("show", t)
        else:
            continue
        events_by_entity.setdefault(key, []).append(rec["ts"])
        names.setdefault(key, key[1])
    return events_by_entity, names


def build_movie_groups(base: Path, events_by_entity: dict) -> dict:
    """entity → franchise pool name from movie_files.parquet collection_name."""
    coll_by_tmdb: dict = {}
    for path in glob.glob(str(base / "radarr" / "*" / "movie_files.parquet")):
        try:
            df = pd.read_parquet(path, columns=["tmdb_id", "collection_name", "title"])
        except Exception:
            continue
        for rec in df.to_dict("records"):
            try:
                tmdb = int(rec["tmdb_id"])
            except (TypeError, ValueError):
                continue
            coll = rec.get("collection_name")
            if coll and not (isinstance(coll, float) and pd.isna(coll)):
                coll_by_tmdb[tmdb] = f"collection:{normalize_title(coll)}"
    groups: dict = {}
    for key in events_by_entity:
        kind, ident = key
        if kind == "movie" and ident in coll_by_tmdb:
            groups[key] = coll_by_tmdb[ident]
    return groups


def print_curve(label: str, hazard: np.ndarray, at_risk, bucket_days: int,
                max_rows: int = 12) -> None:
    print(f"\n   {label}")
    print(f"   {'days':>10} {'hazard':>8} {'at_risk':>8}  curve")
    for b in range(min(len(hazard), max_rows)):
        lo, hi = b * bucket_days, (b + 1) * bucket_days
        bar = "█" * int(round(float(hazard[b]) * 40))
        ar = f"{int(at_risk[b]):>8}" if at_risk is not None else "        "
        print(f"   {f'{lo}-{hi}':>10} {hazard[b]:>8.3f} {ar}  {bar}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket-days", type=int, default=7)
    ap.add_argument("--max-days", type=int, default=364)
    ap.add_argument("--k", type=float, default=5.0, help="empirical-Bayes prior strength")
    ap.add_argument("--horizon", type=float, default=14.0,
                    help="window for the residual-probability examples")
    ap.add_argument("--min-pct", type=float, default=50.0,
                    help="min percent_complete for an event to count as a watch")
    ap.add_argument("--top", type=int, default=6, help="most-rewatched entities to print")
    ap.add_argument("--cache-base", default=None)
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args(argv)

    base = Path(args.cache_base) if args.cache_base else CacheKeyBuilder().base_dir
    events = load_watch_events(base)
    if events.empty:
        print(f"No Tautulli history at {base / 'tautulli' / 'history' / 'all.json'}.")
        return 1
    events_by_entity, _names = build_events_by_entity(events, args.min_pct)
    groups = build_movie_groups(base, events_by_entity)
    now = datetime.now(tz=timezone.utc)
    model = fit_survival_model(events_by_entity, now=now, groups_by_entity=groups,
                               k=args.k, bucket_days=args.bucket_days,
                               max_days=args.max_days)

    from scripts.managers.machine_learning.likelihood.survival import hazard_curve
    all_gaps: list = []
    all_cens: list = []
    for ents in events_by_entity.values():
        from scripts.managers.machine_learning.likelihood.survival import entity_gaps
        g, c, _ = entity_gaps(ents, now)
        all_gaps.extend(g)
        all_cens.append(c)
    _, at_risk = hazard_curve(all_gaps, all_cens, args.bucket_days, args.max_days)

    n_events = int(model.household_events)
    n_entities = len(events_by_entity)
    print("=" * 78)
    print("SURVIVAL / RECENCY — discrete-time next-watch hazard (household-pooled)")
    print("=" * 78)
    print(f"   events={n_events} (pct>={args.min_pct:.0f})  entities={n_entities}  "
          f"complete gaps={len(all_gaps)}  titles with own curve={len(model.titles)}  "
          f"franchise pools={len(model.groups)}  k={args.k}")
    if n_events < 100:
        print("   ⚠ tiny sample — curves are the household prior more than title truth.")
    print_curve("HOUSEHOLD hazard (P(next watch in bucket | none yet)):",
                model.household, at_risk, args.bucket_days)

    print(f"\n   residual P(watch within {args.horizon:.0f}d | gap so far) — household pool:")
    for gap in (0, 7, 14, 30, 60, 120):
        from scripts.managers.machine_learning.likelihood.survival import (
            residual_probability_from_hazard,
        )
        p = residual_probability_from_hazard(model.household, gap, args.horizon,
                                             args.bucket_days)
        print(f"     gap {gap:>4}d → {p:.1%}")

    rewatched = sorted(((k, len(v)) for k, v in events_by_entity.items()),
                       key=lambda kv: -kv[1])[:args.top]
    print(f"\n   top rewatched entities (blended title curves, first 6 buckets):")
    for key, n in rewatched:
        curve = model.curve_for(key)
        head = "  ".join(f"{v:.2f}" for v in curve[:6])
        p14 = model.residual_watch_probability(key, 0, args.horizon)
        label = f"{key[0]}:{key[1]}"
        print(f"     {label:<40.40} events={n:<3} P(within {args.horizon:.0f}d)={p14:.1%}  h[:6]={head}")

    if not args.no_write:
        out_dir = base / "ml" / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"survival_{now:%Y-%m-%d}.json"
        out_path.write_text(json.dumps({
            "generated_at": now.isoformat(),
            "n_events": n_events,
            "n_entities": n_entities,
            "bucket_days": args.bucket_days,
            "max_days": args.max_days,
            "k": args.k,
            "household_hazard": [round(float(v), 5) for v in model.household],
            "household_at_risk": [int(v) for v in at_risk],
            "titles_with_own_curve": len(model.titles),
            "franchise_pools": len(model.groups),
        }, indent=2), encoding="utf-8")
        print(f"\nReport written: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
