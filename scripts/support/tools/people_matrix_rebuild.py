"""people_matrix_rebuild — rebuild the person↔media matrix and report what's in it.

The matrix is normally built inside ``main.py`` (right after ``radarr.run()``), so the only
way to refresh it was a full run. That is a lot of machinery to invoke when all you want is
to pick up new credit buckets and see whether the household affinity vector came out sane.

This does just the build, plus a readout that answers the questions that actually matter
after a credits migration:

  * how many titles the matrix covers, split movie/show;
  * whether the movie half is in ONE id space (the check that fails if Trakt-sourced and
    Radarr-sourced credits are mixed — Trakt assigns sequential per-title person ids, so a
    mixed corpus gives the same actor two ids);
  * who the household's top people actually are, BY NAME — the sanity check that the
    affinity vector reflects the household rather than whatever cast happened to be
    mis-filed under the wrong title.

    python scripts/support/tools/people_matrix_rebuild.py
    python scripts/support/tools/people_matrix_rebuild.py --force --top 25
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.managers.factories.cache import GlobalCacheManager                # noqa: E402
from scripts.managers.factories.config.config_loader import ConfigLoader       # noqa: E402
from scripts.managers.factories.daemons.daemon_paths import CONFIG_PATH        # noqa: E402
from scripts.support.utilities.logger.logger import LoggerManager              # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Rebuild the people matrix and report on it")
    ap.add_argument("--force", action="store_true",
                    help="Rebuild even if the fingerprint says nothing changed")
    ap.add_argument("--top", type=int, default=15, help="How many top people to list")
    args = ap.parse_args()

    logger = LoggerManager()
    cfg = ConfigLoader(CONFIG_PATH).load() or {}
    gc = GlobalCacheManager(logger=logger, config=cfg)

    from scripts.managers.services.trakt.people_matrix import TraktPeopleMatrixManager
    mgr = TraktPeopleMatrixManager(logger=logger, config=cfg, global_cache=gc, dry_run=False)

    print("Building people matrix...")
    mgr.build(force=args.force)

    fwd_raw = gc.get("people_matrix/forward") or {}
    aff_raw = gc.get("people_matrix/affinity") or {}
    names   = gc.get("people_matrix/names") or {}
    print(f"\n  titles in matrix : {len(fwd_raw):,}")
    movies = sum(1 for k in fwd_raw if str(k).startswith("movie:"))
    shows  = sum(1 for k in fwd_raw if str(k).startswith("show:"))
    print(f"    movies {movies:,}   shows {shows:,}")
    print(f"  weighted people  : {len(aff_raw):,}")

    if not aff_raw:
        print("\n  Affinity vector is EMPTY — Group-C4 forces its cap to 0.0 when this "
              "happens, which silently disables people_affinity everywhere.")
        return 1

    # Top household people, by name. A migration that went wrong shows up here as
    # unfamiliar names far more legibly than as a count.
    top = sorted(aff_raw.items(), key=lambda kv: float(kv[1]), reverse=True)[:args.top]
    print(f"\n  Household top {len(top)} people:")
    for pid, w in top:
        print(f"    {float(w):7.2f}  {names.get(str(pid)) or names.get(pid) or f'(id {pid})'}")

    # One id space? Trakt's per-title sequential ids collide with TMDB's, so a mixed
    # movie corpus shows the same name under two ids.
    from collections import defaultdict
    by_name = defaultdict(set)
    for pid, nm in (names or {}).items():
        by_name[nm].add(str(pid))
    dupes = {k: v for k, v in by_name.items() if len(v) > 1}
    print(f"\n  distinct people names: {len(by_name):,}")
    if dupes:
        print(f"  WARNING {len(dupes)} name(s) hold more than one id — mixed id spaces:")
        for k, v in list(dupes.items())[:5]:
            print(f"      {k}: {sorted(v)}")
        print("  Run radarr_credits_sync.py --sync --force --prune-foreign, then rebuild.")
    else:
        print("  Every name maps to one id — single id space.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
