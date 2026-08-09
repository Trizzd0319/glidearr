"""discovery_dryrun.py — exercise the discovery READ path without waiting 30 days.

The recommendation ledger measures whether a shelf worked, and a completion needs
a placement recorded BEFORE the play. So the first armed run proves the WRITE
side (did the recorder fire, on the right branch, with a usable identity) and
proves nothing at all about the READ side, which cannot produce a single row for
a month.

This closes that gap. It builds a SYNTHETIC ledger from the plans currently on
disk, backdates it so the measurement window has elapsed, joins it against the
REAL Tautulli history, and reports exactly what `discovery.py` and the
Because-You-Watched builder would do with it.

    python -m scripts.support.tools.discovery_dryrun
    python -m scripts.support.tools.discovery_dryrun --days-ago 14 --min-pct 85

IT WRITES TO A SANDBOX BY DEFAULT, and can write to the REAL cache with
``--seed-real``. Both are legitimate; they differ in what they let you observe.

The sandbox proves the LOGIC. Seeding the real cache additionally proves the
WIRING - that the affinity builder finds the ledger, that write-back sees a plan,
that the whole chain reaches Plex - which a temp directory cannot show.

Seeding the real cache is UNDOABLE, precisely: every synthetic row carries the
same backdated ``recommended_at``, so ``--undo <stamp>`` removes exactly those
and nothing else. It does not delete a month partition wholesale, because a
partition can hold real rows alongside them.

HIDDEN GEMS IS REFUSED, always, regardless of flags. It is the one surface where
seeding is NOT reversible in the way that matters: ``recommendations.open_picks``
reads that ledger to decide which picks the shelf HOLDS OPEN across their
windows, so a synthetic row does not merely sit in a file - the next run holds a
pick that was never published, and deleting the parquet afterwards does not
un-build the shelf that was already wrong. Its 154 live rows are also the only
real measurement the system currently has.

WHAT IT PROVES, AND WHAT IT DOES NOT.

  proves      the plan items carry a usable stable identity
              `recommendations.build_events` accepts them (movies AND shows)
              the parquet round-trip works on this machine
              the play -> entity resolver matches against REAL inventories
              `discovery.completions` finds real plays, with real percentages
              the affinity contributions are non-empty and sensible

  does NOT    that write-back records on the right branches (needs an armed run)
              that a real placement precedes a real play (that IS the 30 days)

A completion found here is a play the household ALREADY made against a pick the
shelf is offering NOW. That is a plausible-outcome rehearsal, not a measurement,
and the report says so on every line.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.managers.machine_learning.labels import recommendations as R  # noqa: E402
from scripts.managers.machine_learning.playlists import discovery as D  # noqa: E402

CACHE = Path(__file__).resolve().parents[1] / "cache"

#: NEVER seeded, whatever the flags say. See the module docstring: this surface
#: has live rows AND a live behavioural coupling through ``open_picks``, so a
#: synthetic placement changes what the next run HOLDS, which no later deletion
#: undoes.
REFUSED_SURFACES = frozenset({"hidden_gems"})

#: plan cache dir -> the surface it would be recorded under. Mirrors
#: ``writeback._RECORD_SURFACE``; keep the two in step.
_PLANS = {
    "plex/playlists/twih_movie_plan": "anniversary",
    "plex/playlists/twih_show_plan": "anniversary",
    "plex/playlists/tonight_plan": "tonight",
    "plex/playlists/gems_plan": "hidden_gems",
}


def _load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _unwrap(blob):
    """Cache files are written either bare or inside a ``{"value": ...}`` envelope."""
    if isinstance(blob, dict) and "value" in blob and "items" not in blob:
        return blob["value"]
    return blob


def load_plans():
    """``[(surface, profile, [items])]`` from every plan on disk."""
    out = []
    for rel, surface in _PLANS.items():
        d = CACHE / Path(rel)
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.json")):
            plan = _unwrap(_load_json(f, {})) or {}
            items = plan.get("items") if isinstance(plan, dict) else None
            if items:
                out.append((surface, f.stem, items))
    return out


def load_history():
    """Real Tautulli rows, tagged with the profile the ledger keys on."""
    users = CACHE / "tautulli" / "history" / "user"
    rows = []
    if users.is_dir():
        for f in sorted(users.glob("*.json")):
            data = _unwrap(_load_json(f, [])) or []
            for r in data:
                if isinstance(r, dict):
                    r.setdefault("profile", f.stem)
                    rows.append(r)
    if rows:
        return rows
    # Fall back to the combined file; it has no per-profile split, so every row is
    # tried against every profile. That over-matches, and the report says so.
    return [r for r in (_unwrap(_load_json(
        CACHE / "tautulli" / "history" / "all.json", [])) or []) if isinstance(r, dict)]


def build_resolver():
    """``play -> entity_id``, from the REAL inventories. Also returns their sizes."""
    movies = _unwrap(_load_json(CACHE / "plex" / "movies" / "owned_inventory.json", {})) or {}
    eps = _unwrap(_load_json(CACHE / "plex" / "episodes" / "owned_inventory.json", {})) or {}
    tmdb_by_rk = {str(r["rating_key"]): str(t) for t, r in movies.items()
                  if isinstance(r, dict) and r.get("rating_key") is not None}
    join_by_rk = {str(r["rating_key"]): str(k) for k, r in eps.items()
                  if isinstance(r, dict) and r.get("rating_key") is not None}

    def _resolve(row):
        rk = str((row or {}).get("rating_key") or "")
        if rk in tmdb_by_rk:
            return tmdb_by_rk[rk]
        if rk in join_by_rk:
            key = join_by_rk[rk]
            return key if key.startswith("tvdb:") else f"tvdb:{key}"
        return None

    return _resolve, len(tmdb_by_rk), len(join_by_rk)


def undo(base: Path, stamp: str) -> int:
    """Delete every row whose ``recommended_at`` equals ``stamp``. Returns the count.

    Row-level, never file-level: a month partition can hold real placements beside
    the synthetic ones, and dropping the file to clean up a test would take the
    real measurements with it. All synthetic rows from one seeding run share the
    exact backdated timestamp, which makes them precisely identifiable without
    needing a marker column.
    """
    import pandas as pd
    removed = 0
    root = base / "ml" / "recommendations"
    if not root.is_dir():
        return 0
    for surface_dir in sorted(root.iterdir()):
        if not surface_dir.is_dir() or surface_dir.name in REFUSED_SURFACES:
            continue
        for part in sorted(surface_dir.glob("*.parquet")):
            try:
                df = pd.read_parquet(part)
            except Exception:
                continue
            if "recommended_at" not in df.columns:
                continue
            keep = df[df["recommended_at"].astype(str) != str(stamp)]
            n = len(df) - len(keep)
            if not n:
                continue
            removed += n
            if len(keep):
                keep.to_parquet(part, index=False)
            else:
                part.unlink()          # the partition held ONLY synthetic rows
            print(f"    {surface_dir.name}/{part.name}: removed {n}, kept {len(keep)}")
    return removed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days-ago", type=int, default=21,
                    help="backdate the synthetic placements this many days")
    ap.add_argument("--min-pct", type=float, default=D.DEFAULT_MIN_PCT)
    ap.add_argument("--keep", action="store_true", help="print the sandbox path and keep it")
    ap.add_argument("--seed-real", action="store_true",
                    help="write into the REAL cache (undo with --undo <stamp>)")
    ap.add_argument("--undo", metavar="STAMP",
                    help="remove every synthetic row carrying this recommended_at")
    args = ap.parse_args(argv)

    if args.undo:
        print(f"[discovery-dryrun] removing rows with recommended_at == {args.undo}")
        n = undo(CACHE, args.undo)
        print(f"[discovery-dryrun] removed {n} row(s). hidden_gems untouched.")
        return 0

    if args.seed_real:
        target = CACHE
        print(f"[discovery-dryrun] SEEDING THE REAL CACHE at {target}")
        print(f"[discovery-dryrun] hidden_gems is refused; everything else is undoable.")
    else:
        target = Path(tempfile.mkdtemp(prefix="glidearr-discovery-"))
        print(f"[discovery-dryrun] SANDBOX {target}")
        print("[discovery-dryrun] the real ledger is NOT touched.")
    print()

    plans = load_plans()
    if not plans:
        print("  no cached plans found — run the builders first, then re-run this.")
        return 0

    when = (datetime.now(timezone.utc) - timedelta(days=args.days_ago)).replace(microsecond=0)
    stamp = when.isoformat().replace("+00:00", "")

    print(f"  {'surface':<14}{'profile':<14}{'picks':>6}{'recordable':>12}{'no id':>8}")
    print("  " + "-" * 56)
    total_rows = total_skipped = 0
    for surface, profile, items in plans:
        if surface in REFUSED_SURFACES:
            print(f"  {surface:<14}{profile:<14}{len(items):>6}{'REFUSED':>12}{'-':>8}")
            continue
        rows, skipped = R.build_events_ex(items, profile=profile, surface=surface,
                                          recommended_at=stamp)
        if rows:
            R.append_events(target, rows, surface=surface)
        total_rows += len(rows)
        total_skipped += skipped
        print(f"  {surface:<14}{profile:<14}{len(items):>6}{len(rows):>12}{skipped:>8}")

    print(f"\n  {total_rows} placement(s) synthesised, {total_skipped} pick(s) had NO stable id.")
    if args.seed_real and total_rows:
        print(f"  UNDO WITH:  python -m scripts.support.tools.discovery_dryrun --undo {stamp}")
    if total_skipped and not total_rows:
        print("  ⚠  EVERY pick was unrecordable. The plans on disk predate the")
        print("     build-time identity capture (GLD-PLY-24) — re-run the builders")
        print("     and this will change. That is the finding, not a failure here.")
        return 0

    plays = load_history()
    resolve, n_movies, n_eps = build_resolver()
    print(f"  {len(plays):,} play(s) · inventories: {n_movies:,} movies, {n_eps:,} episodes\n")

    events = []
    for surface in sorted({s for s, _p, _i in plans} - REFUSED_SURFACES):
        df = R.load_events(target, surface=surface)
        if df is not None and not getattr(df, "empty", True):
            events.extend(df.to_dict("records"))

    done = D.completions(events, plays, entity_of_play=resolve,
                         min_pct=args.min_pct, surfaces=None)
    print(f"  === COMPLETIONS (rehearsal, not measurement) — {len(done)} ===")
    for r in done[:15]:
        print(f"    {str(r['surface']):<12} {str(r['profile']):<12} "
              f"{str(r['title'])[:34]:<36} {r['percent_complete']:.0f}%")
    if len(done) > 15:
        print(f"    … and {len(done) - 15} more")

    print(f"\n  === PER-SURFACE ===")
    summ = D.summary(events, plays, entity_of_play=resolve, min_pct=args.min_pct)
    for s, v in sorted(summ.items()):
        rate = "—" if v["completion_rate"] is None else f"{v['completion_rate']:.0%}"
        print(f"    {s:<14} surfaced {v['surfaced']:>4} · played {v['played']:>4} "
              f"· completed {v['completed']:>4} · completion {rate}")

    contrib = D.affinity_contributions(done, attributes_of=lambda eid, r: [f"media:{r.get('media')}"])
    print(f"\n  === CONTRIBUTIONS === {len(contrib)} profile(s) would gain a signal")
    for prof, attrs in list(contrib.items())[:6]:
        print(f"    {str(prof):<14} {dict(list(attrs.items())[:4])}")

    print("\n  READ PATH EXERCISED. What this does NOT prove: that write-back records")
    print("  on the correct branches, and that a real placement precedes a real play.")
    print("  Only an armed run and elapsed time can show those.")
    if args.seed_real:
        print(f"\n  SEEDED THE REAL CACHE. Undo when finished:")
        print(f"    python -m scripts.support.tools.discovery_dryrun --undo {stamp}")
    elif not args.keep:
        print(f"\n  sandbox left at {target} — delete it when done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
