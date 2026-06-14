# `trakt/shows` package

**File** — `scripts/managers/services/trakt/shows/__init__.py`
**One-liner** — Package marker for the TV-show side of the Trakt adapter: a watchability-scoring re-export shim plus a per-`tvdbId` disk-cache reader.

## What it does (for a senior Python engineer)

`__init__.py` here is a pure documentation/marker module — it contains **no manager class and no executable logic** (just a module docstring). It exists so the directory is an importable package and to record, in one place, what the two real modules in the directory are for. It mirrors the movie side (`trakt/movies`, with its `score_movie` / `TraktMovieCacheManager` pair) for the Sonarr (TV) flow.

The package holds two modules:

- `scorer.py` — a **re-export shim** for `score_show()` and friends. The actual 0–100 TV watchability engine moved to the brain layer (`scripts.managers.machine_learning.scoring.show_scorer`) during ML-migration Step 2; the shim keeps the old import path alive. See `scorer.md`. (The brain module itself is out of scope and intentionally undocumented here.)
- `cache.py` — `TraktShowCacheManager`, a gzip-JSON disk-cache reader keyed by Sonarr's `tvdbId` over the enrich daemon's show buckets (people / ratings / related). See `cache.md`.

FETCH / CACHE / APPLY: none at the package level. No config keys, no `global_cache` / Parquet keys, no API endpoints, no `dry_run` behavior. Importing the package has no side effects beyond binding the docstring.

## How it functions

There is no lifecycle here. Python imports the package, binds the module docstring, and the two submodules are imported on demand by their consumers (the Sonarr `episode_files` cache manager imports both; see `cache.md`).

## Criteria & examples

No thresholds or guards live in `__init__.py`. The scoring thresholds live in the brain (`show_scorer`, out of scope) and the freshness TTL lives in `TraktShowCacheManager` (`cache.md`).

## In plain English

Think of this folder as the labelled drawer for everything Trakt knows about TV shows. The drawer itself does nothing — it just holds two tools: one that decides how worth-watching a show is (the scorer, which now actually lives in the "brain" elsewhere), and one that quickly looks up the cast, ratings, and similar-show notes Trakt previously saved to disk (the cache reader). The label exists so anyone opening the codebase knows which drawer to reach into.

## Interactions

- **Parent of the modules:** the scorer shim points into `machine_learning.scoring.show_scorer` (brain); the cache module declares `parent_name = "TraktShowsManager"` for the `BaseManager` deferred parent link.
- **Real consumer:** `scripts/managers/services/sonarr/cache/episode_files.py` imports both `score_show` (via the shim) and `TraktShowCacheManager`.
