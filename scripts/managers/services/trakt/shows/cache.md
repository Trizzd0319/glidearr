# TraktShowCacheManager

**File** — `scripts/managers/services/trakt/shows/cache.py`
**One-liner** — A read-only, per-`tvdbId` gzip-JSON disk-cache reader for the Trakt show data (credits, ratings, related shows) that the enrich daemon writes; the TV twin of `TraktMovieCacheManager`.

## What it does (for a senior Python engineer)

`TraktShowCacheManager(BaseManager, ComponentManagerMixin)` is a thin **read adapter** over files the background enrich daemon previously wrote to disk. It does **not** call Trakt itself — it only opens gzip-compressed JSON files keyed by Sonarr's `tvdbId`. In FETCH / CACHE / APPLY terms it is a pure cache *reader* (a degraded form of FETCH against the local disk cache): it performs **no HTTP**, writes nothing, and never PUT/POST/DELETEs. It mixes in `ComponentManagerMixin` but does **not** call `load_components`, so it loads no submanagers.

**Directory layout** (single-sourced from `daemon_paths.SHOW_BUCKETS` so daemon and runtime never diverge):

| Bucket    | Path                                          | Payload shape                              |
|-----------|-----------------------------------------------|--------------------------------------------|
| `people`  | `cache/trakt/shows/{tvdbId}.json.gz`          | `{cast, crew}` credits                     |
| `ratings` | `cache/trakt/show_ratings/{tvdbId}.json.gz`   | `{rating, votes, distribution}`            |
| `related` | `cache/trakt/show_related/{tvdbId}.json.gz`   | list of related-show objects `{ids, title, year}` |

The `ratings` and `related` keys are resolved with `SHOW_BUCKETS.get(...)` defaulting next to the `people` bucket, so an older daemon build that never wrote those buckets degrades to a clean cache-miss instead of crashing.

**Public methods:**
- `is_fresh(tvdb_id, bucket="people") -> bool` — True iff the bucket file for that id exists and its mtime is within `self.ttl`.
- `get_people(tvdb_id) -> dict` — returns the `{cast, crew}` credits dict, or `{}` if missing/stale/unreadable/`tvdb_id` falsy. Mirrors `TraktMoviePeopleManager.get_people` so the scorer's Group-B affinity call-site is identical for movies and shows. Decorated `@timeit("get_people")`.
- `get_ratings(tvdb_id) -> dict` — returns the `{rating, votes, distribution}` dict or `{}`. Decorated `@timeit("get_ratings")`.
- `get_related(tvdb_id) -> list` — returns the list of Trakt-related neighbour show objects (each `{"ids": {"tvdb": ...}, "title": ..., "year": ...}`) or `[]`. Feeds the Group-C3 related-graph affinity term. Because the daemon negative-caches an empty `{}` for shows with no related data, a non-list payload is coerced to `[]`. Decorated `@timeit("get_related")`.
- `stats() -> dict` — per-bucket `{total, fresh, stale}` counts by globbing `*.json.gz` and comparing mtimes to `self.ttl`.

**Manager-tree position:** sets `self.parent_name = "TraktShowsManager"` so `BaseManager`'s deferred resolver will auto-link to a `TraktShowsManager` parent if/when present, inheriting its logger/config/cache/validator. In the live code path, however, it is **constructed directly** (not as a `load_components` component) by the Sonarr `episode_files` cache manager — see Interactions. As a `BaseManager` it is a process-wide singleton cached by `(class, singleton_key)`.

**Config keys read:** none directly. Construction-time `kwargs`: `ttl` (defaults to `DEFAULT_TTL = CACHE_TTL_S = 604_800` s = 7 days, matching the daemon's `CACHE_TTL_S`) and `dry_run` (captured from `kwargs["dry_run"]`, else the parent's `dry_run`, else `False`).

**`global_cache` / Parquet:** reads/writes none. It reads only the on-disk `*.json.gz` files under `SHOW_BUCKETS`.

**dry_run:** captured into `self.dry_run` but functionally inert here — this is a read-only manager, so there is nothing to suppress. (It is stored so a future write path would honor it.)

**Concurrency/threading:** no locks, no threads. Reads are stateless per call; the only mutable state is the cached singleton itself and the lazily-cached directory map `self._dirs`.

## How it functions

Lifecycle: `__init__` sets `parent_name`, calls `super().__init__(...)` (BaseManager dependency injection + deferred parent link), `self.register()`, captures `dry_run` and `ttl`, then builds `self._dirs` — a `{bucket: Path}` map from `SHOW_BUCKETS` with the safe defaults described above — and logs a single debug line reporting the three resolved paths and the TTL. There is no `load_components` step and no long-running `run` entry point; callers invoke the `get_*` accessors on demand.

Internal helpers:
- `_path(bucket, tvdb_id)` — `self._dirs[bucket] / f"{tvdb_id}.json.gz"`.
- `_read(bucket, tvdb_id)` — the freshness-gated reader: returns `None` if the file is missing, `None` if `(now - mtime) > ttl` (stale), otherwise `gzip.open(...)` + `json.load`; any exception is swallowed to a debug log and returns `None`. All three public getters funnel through `_read` and coerce its `None` to the empty value appropriate for their return type (`{}` / `{}` / `[]`).

**Delegation:** this manager makes no value judgements — it just hands raw cached data to the consumer. The decisions that consume this data (the TV watchability score) live in the brain module `machine_learning.scoring.show_scorer` (out of scope and not documented here).

## Criteria & examples

- **Freshness / TTL gate (7 days).** A `people` file last modified 5 days ago (`now - mtime = 432_000 s < 604_800`) is served; `get_people` returns its `{cast, crew}`. The same file last modified 8 days ago (`691_200 s > 604_800`) is treated as a miss — `_read` returns `None`, `get_people` returns `{}`, and the scorer simply scores that show with no Group-B credit affinity rather than erroring.
- **Falsy id guard.** `get_people(0)` / `get_ratings(None)` short-circuit before any disk access and return the empty value, so a Sonarr series with no `tvdbId` never triggers a file lookup.
- **Missing-bucket degradation.** On an older daemon that never created `show_related/`, `_dirs["related"]` still resolves (to `…/show_related` next to `shows/`); `get_related(123456)` finds no file and returns `[]` — the Group-C3 related-graph term contributes nothing instead of crashing.
- **Negative-cache coercion.** If the daemon wrote `cache/trakt/show_related/123456.json.gz` containing `{}` (its "no related data" sentinel) rather than a list, `get_related` sees a non-`list` payload and returns `[]`.
- **`stats()` example.** With 200 files in the `people` dir of which 160 have mtimes within 7 days, `stats()["people"] == {"total": 200, "fresh": 160, "stale": 40}`.

## In plain English

Imagine you wanted to decide whether to keep *The Mandalorian* on your shelf. To judge it fairly you'd like to know who's in it, its audience rating, and what similar shows exist. A helper already did the legwork earlier and jotted those notes on index cards filed by show ID. This class is the clerk who fetches the right card fast. If a card is older than a week the clerk treats it as out-of-date and hands back a blank one (better to skip a stale fact than trust it); if the card was never written, the clerk just shrugs and returns "nothing" instead of panicking. The clerk never phones Trakt directly and never edits the cards — it only reads them.

## Interactions

- **Declared parent:** `TraktShowsManager` (via `parent_name`, resolved lazily by `BaseManager`). No submanagers of its own.
- **Actual constructor / consumer:** `scripts/managers/services/sonarr/cache/episode_files.py` — its `_get_show_cache()` lazily instantiates this manager (passing `logger`, `config`, `global_cache`, `registry`, `dry_run`) and then calls `get_people`, `get_ratings`, and `get_related` per show while building the per-series feature row.
- **Sibling module:** `scorer.py` (the `score_show` shim) — the scorer consumes the dicts/lists this manager returns.
- **Brain module it feeds (noted, not documented):** `machine_learning.scoring.show_scorer` (Group-B credit affinity, Group-F critic blend from ratings, Group-C3 related-graph affinity).
- **Data producer:** the enrich daemon, which writes the `SHOW_BUCKETS` files this manager reads; paths and the 7-day `CACHE_TTL_S` are single-sourced from `scripts/managers/factories/daemons/daemon_paths.py`.
