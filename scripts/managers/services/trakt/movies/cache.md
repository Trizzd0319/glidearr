# TraktMovieCacheManager

- **File** — `scripts/managers/services/trakt/movies/cache.py`
- **One-liner** — A per-TMDb-ID, gzip-compressed, TTL-bounded disk cache for raw Trakt movie data (people/credits) shared by the runtime scorer and the background enrichment daemon.

## What it does (for a senior Python engineer)

`TraktMovieCacheManager(BaseManager, ComponentManagerMixin)` is a leaf submanager that owns nothing but a directory of small gzipped JSON files. It is the CACHE arm of the FETCH/CACHE/APPLY split: it never makes HTTP calls and never makes value judgements — it only persists and serves raw normalised Trakt responses.

Layout / location:
- Files are written as `{tmdb_id}.json.gz` (gzip-compressed JSON, minified separators) inside `base_dir`.
- `DEFAULT_DIR = MOVIE_BUCKETS["people"]` from `factories/daemons/daemon_paths.py`, which resolves to the absolute path `…/scripts/support/cache/trakt/movies`. This is single-sourced with `enrich_daemon.py` so the daemon's writes and the runtime scorer's reads hit the exact same directory (a prior bug used a CWD-relative path that diverged).
- `DEFAULT_TTL = CACHE_TTL_S = 604_800` seconds (7 days). Freshness is judged purely by file mtime vs. TTL — there is no in-file timestamp.

Construction args (via `kwargs`): `ttl` (override TTL, coerced to int), `cache_dir` (override `base_dir`), `dry_run`, `manager` (parent). In non-dry-run mode `__init__` creates `base_dir` with `mkdir(parents=True, exist_ok=True)`.

Key PUBLIC methods:
- `is_fresh(tmdb_id) -> bool` — True iff the file exists AND `(now - mtime) <= ttl`.
- `get(tmdb_id) -> dict | None` — returns parsed cached data, or None if missing/expired/unreadable. Calls `is_fresh` then reads.
- `get_fresh(tmdb_id) -> tuple[bool, dict | None]` — single-pass freshness check + read returning `(was_fresh, data)`. `was_fresh` mirrors `is_fresh` exactly; `data` is the parsed dict or None. A fresh-but-corrupt file yields `(True, None)`. This lets callers derive both signals from one stat + read instead of an `is_fresh` stat followed by a redundant `get`. Used heavily by `TraktMoviePeopleManager.enrich_movies`.
- `set(tmdb_id, data) -> bool` — atomically writes via `tempfile.mkstemp` in the same dir + `os.replace`, so a hard kill (e.g. daemon termination) can never leave a truncated `.gz` for a later reader. No-ops and returns `False` in dry_run.
- `invalidate(tmdb_id)` — `unlink(missing_ok=True)` on the file.
- `stats() -> dict` — globs `*.json.gz` and returns `{"total", "fresh", "stale"}` counts by mtime vs. TTL.

Manager tree: parent is `TraktMoviesManager` (`parent_name = "TraktMoviesManager"`). It loads no submanagers of its own (it inherits `ComponentManagerMixin` but does not call `load_components`). It is constructed once by `TraktMoviesManager` and that single instance is injected into `TraktMoviePeopleManager` as `cache_manager`, so people and the parent share one cache instance.

FETCH/CACHE/APPLY: CACHE only.

External API endpoints: none.

Config keys read: none directly (TTL/dir come from `daemon_paths` constants or kwargs). Note: as a `BaseManager` singleton it still receives the injected `config`, but this class does not read it.

global_cache / Parquet keys: none. This cache is a private gzip-file directory, not the shared `global_cache`.

dry_run: `set` is a no-op returning `False`; `__init__` skips directory creation. Reads (`get`/`get_fresh`/`stats`) are unaffected.

Singleton / concurrency: like every `BaseManager` it is a process-wide singleton keyed by `(class, singleton_key)`. Atomic temp-file-then-replace makes concurrent writers (daemon vs. main run) safe at the file level; there is no in-process lock.

## How it functions

Lifecycle is trivial: `__init__` → `super().__init__` (injects shared deps, auto-links to parent `TraktMoviesManager`) → `register()` → resolve `ttl`/`base_dir` → create the dir unless dry_run. There is no `run`/entry method; it is a passive store driven by callers. Internal helper `_path(tmdb_id)` builds the `{tmdb_id}.json.gz` path. No decision is delegated to a machine_learning brain module — this class makes no decisions.

## Criteria & examples

- **Freshness window (7 days).** A file last modified 6 days ago: `now - mtime ≈ 518 400 s ≤ 604 800 s` → `is_fresh` True, `get` returns the data. The same file 8 days later: `≈ 691 200 s > 604 800 s` → `is_fresh` False, `get` returns None (a miss that forces a re-fetch upstream).
- **Fresh-but-corrupt split.** If `tmdb_id=27205` (Inception) was written 1 day ago but the gz is truncated, `get_fresh(27205)` returns `(True, None)`: the entry is "fresh" (don't re-fetch on cadence) yet has no usable data — matching `is_fresh()=True` while `get()=None`.
- **dry_run write.** `set(603, credits)` (The Matrix) in dry_run returns `False` and writes nothing; the next `get(603)` is a miss.

## In plain English

Think of this as a labeled shoebox of index cards, one card per movie (each filed under the movie's TMDb number — say card "27205" for *Inception*). Each card lists who was in the movie and who made it. Cards older than a week are considered out of date and ignored until refreshed. When the app needs *Inception*'s cast, it grabs the card from the box instead of phoning the movie database again — fast and polite to the outside service. The box is written carefully so that if the power is cut mid-write, you never end up with a half-written, unreadable card. In "dry run" mode the app only reads cards and never writes new ones, so you can preview what it would do without changing anything.

## Interactions

- **Parent:** `TraktMoviesManager` (constructs this once and shares the instance).
- **Primary consumer:** sibling `TraktMoviePeopleManager`, which receives this instance as `cache_manager` and reads via `get` / `get_fresh` and writes via `set`.
- **Out-of-process peer:** `scripts/support/daemons/enrich_daemon.py` writes the same `…/trakt/movies` directory (path single-sourced through `MOVIE_BUCKETS["people"]`), so the daemon can fill the cache that this manager serves.
- **Brain modules:** none — this is pure storage.
