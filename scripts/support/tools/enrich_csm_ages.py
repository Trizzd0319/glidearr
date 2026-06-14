"""
enrich_csm_ages.py — manual catch-up for the Common Sense Media age cache.
================================================================================
The enrich DAEMON fills these caches incrementally every cycle (``run_commonsense`` for
movies, ``run_commonsense_tv`` for shows); this tool bulk-fills faster on demand
(concurrent) and shares the exact cache format via ``managers/services/mdblist/age_cache``.
TWO caches, because movie and show tmdbIds share an integer space and must not collide:

    support/cache/mdblist/age_ratings.json    = { "<movie tmdbId>": <age int> | null }
    support/cache/mdblist/age_ratings_tv.json = { "<show  tmdbId>": <age int> | null }

The movie classifier + the per-profile playlist cert-gate read them as the kids signal.
Resumable (skips ids already cached), budget-aware (stops with reserve), owned-first.
Standalone — keyring + hardcoded Radarr/Sonarr maps, no config.json.

NOTE: stop the enrich daemon while bulk-filling so the two don't race on the cache files
(the daemon checkpoints every cycle and could clobber a long bulk run's progress).

Usage:
    python scripts/support/tools/enrich_csm_ages.py                 # fill BOTH up to budget
    python scripts/support/tools/enrich_csm_ages.py --media tv      # TV only
    python scripts/support/tools/enrich_csm_ages.py --media movie --limit 5000 --workers 16
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

try:  # UTF-8 console so glyphs never crash on Windows cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_SCRIPTS_DIR = Path(__file__).resolve().parents[2]          # scripts/
if str(_SCRIPTS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR.parent))
from scripts.managers.services.mdblist import age_cache          # noqa: E402
from scripts.managers.services.mdblist.client import movie_ratings, show_ratings  # noqa: E402

RADARR_BASE = os.environ.get("RECOMMENDARR_RADARR_INSTANCES_STANDARD_BASE_URL") or "http://192.168.1.110:8988"
SONARR_BASE = os.environ.get("RECOMMENDARR_SONARR_INSTANCES_SONARR_BASE_URL") or "http://192.168.1.110:8990"


def _secret(path_key: str) -> str:
    p = _SCRIPTS_DIR / "managers" / "factories" / "config" / "secret_store.py"
    spec = importlib.util.spec_from_file_location("_mr_ss", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    try:
        return mod.SecretStore().get(path_key) or ""
    except Exception:
        return ""


def _radarr_movie_ids() -> list[int]:
    """Owned-first list of Radarr movie tmdbIds (for the movie age cache)."""
    key = _secret("radarr_instances.standard.api")
    if not key:
        sys.exit("No Radarr API key in keyring (radarr_instances.standard.api).")
    movies = requests.get(f"{RADARR_BASE}/api/v3/movie",
                          headers={"X-Api-Key": key}, timeout=300).json()
    rows = [m for m in movies if m.get("tmdbId")]
    rows.sort(key=lambda m: (not m.get("hasFile"), int(m["tmdbId"])))      # owned first
    return [int(m["tmdbId"]) for m in rows]


def _sonarr_series_ids() -> list[int]:
    """Owned-first list of Sonarr series tmdbIds (for the TV age cache). Show and movie
    tmdbIds share an integer space, so these go to a SEPARATE cache file."""
    key = _secret("sonarr_instances.sonarr.api")
    if not key:
        sys.exit("No Sonarr API key in keyring (sonarr_instances.sonarr.api).")
    series = requests.get(f"{SONARR_BASE}/api/v3/series",
                          headers={"X-Api-Key": key}, timeout=300).json()
    rows = [s for s in series if s.get("tmdbId")]
    rows.sort(key=lambda s: (not ((s.get("statistics", {}) or {}).get("episodeFileCount", 0) > 0),
                             int(s["tmdbId"])))                            # owned first
    return [int(s["tmdbId"]) for s in rows]


def _fill(label: str, ids: list[int], cache_path, lookup, apikey: str,
          workers: int, limit: int) -> None:
    """Concurrently fill one age cache (movie or TV) for the uncached ids, budget-capped."""
    cache = age_cache.load(cache_path)
    used, budget = age_cache.budget(apikey)
    headroom = max(0, budget - age_cache.BUDGET_FLOOR - used)
    cap = min(limit or headroom, headroom)
    todo = [t for t in ids if str(t) not in cache][:cap]
    print(f"[{label}] MDBList budget {used:,}/{budget:,} | cache {len(cache):,} ids | "
          f"fetching {len(todo):,} uncached with {workers} workers")
    if not todo:
        print(f"[{label}] nothing to fetch.")
        return
    done = covered = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(lookup, apikey, t): t for t in todo}
        for fut in as_completed(futs):
            t = futs[fut]
            try:
                r = fut.result()
            except Exception:
                continue
            if not r["ok"]:
                continue
            cache[str(t)] = r["age_rating"]
            done += 1
            if r["age_rating"] is not None:
                covered += 1
            if done % 1000 == 0:
                age_cache.save(cache, cache_path)
                print(f"  [{label}] ...{done:,}/{len(todo):,} ({covered:,} with CSM age)")
    age_cache.save(cache, cache_path)
    with_age = sum(1 for v in cache.values() if isinstance(v, int))
    print(f"[{label}] Done. +{done:,} this run ({covered:,} with CSM age). "
          f"Cache now {len(cache):,} ids, {with_age:,} with an age -> {cache_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Bulk-fill the Common Sense age caches from MDBList.")
    ap.add_argument("--media", choices=("movie", "tv", "both"), default="both",
                    help="which age cache(s) to fill (default both; --limit applies per media)")
    ap.add_argument("--limit", type=int, default=0, help="max lookups this run per media (0 = up to daily budget)")
    ap.add_argument("--workers", type=int, default=16, help="concurrent MDBList requests")
    args = ap.parse_args()

    apikey = _secret("mdblist.apikey")
    if not apikey:
        sys.exit("No MDBList API key in keyring (mdblist.apikey).")

    if args.media in ("movie", "both"):
        _fill("movie", _radarr_movie_ids(), age_cache.AGE_CACHE_PATH,
              movie_ratings, apikey, args.workers, args.limit)
    if args.media in ("tv", "both"):
        _fill("tv", _sonarr_series_ids(), age_cache.TV_AGE_CACHE_PATH,
              show_ratings, apikey, args.workers, args.limit)


if __name__ == "__main__":
    main()
