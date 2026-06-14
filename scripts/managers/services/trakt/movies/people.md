# TraktMoviePeopleManager

- **File** — `scripts/managers/services/trakt/movies/people.py`
- **One-liner** — Fetches and caches movie cast/crew from Trakt and injects normalised `credits` onto movie dicts, using budgeted, cursored round-robins so a large library can be enriched over many runs without tripping Trakt's rate limit.

## What it does (for a senior Python engineer)

`TraktMoviePeopleManager(BaseManager, ComponentManagerMixin)` is the FETCH + CACHE workhorse under `TraktMoviesManager`. It performs the only HTTP calls in this subtree, normalises the raw Trakt people payload into the credits shape the Radarr relational layer expects, and persists the normalised result through the shared `TraktMovieCacheManager`. It deliberately delegates the *which movies to enrich this run* decision to a machine_learning brain module (see below) — the manager itself is the thin adapter.

Manager tree: parent is `TraktMoviesManager` (`parent_name = "TraktMoviesManager"`). It loads no submanagers (it inherits `ComponentManagerMixin` but does not call `load_components`). The shared `TraktMovieCacheManager` is injected via the `cache_manager` kwarg; if absent it builds its own standalone cache.

FETCH/CACHE/APPLY:
- **FETCH** — `GET https://api.trakt.tv/movies/{tmdb_id}/people` via a persistent `requests.Session`.
- **CACHE** — writes/reads the normalised credits through `TraktMovieCacheManager`; reads/writes round-robin cursors in `global_cache`.
- **APPLY** — none (it produces no PUT/DELETE/POST against a managed service). Token refresh `POST /oauth/token` is auth maintenance, not a library mutation.

External API endpoints:
- `GET /movies/{tmdb_id}/people` (cast + crew).
- `POST /oauth/token` (OAuth refresh-token grant), persisted back to config.

Config keys read (under `trakt`): `client_id`, `client_secret`, `authorization.access_token`, `authorization.refresh_token`, `authorization.created_at`, `authorization.expires_in`. Also `daemons.enrich.enabled` — when truthy the manager goes **globally cache-only** (`self.cache_only = True`). On a successful refresh it writes the new auth blob back via `self.config.set("trakt", trakt_cfg)`.

global_cache keys (round-robin cursors, read + written):
- `trakt/movie_people/watched_cursor` — id-bisect cursor for uncached watched/priority movies (`{"last_tmdb_id": …}`).
- `trakt/movie_people/chunk_cursor` — relevance cursor for owned non-priority movies (`{"done_ids": [...], "size", "total"}`).
- `trakt/movie_people/unowned_chunk_cursor` — id-bisect cursor for unowned movies.

Key PUBLIC methods:
- `get_people(tmdb_id) -> dict | None` — cache-first read; on miss, if not cache-only, FETCHes `/movies/{id}/people`, normalises, caches, and returns. Returns None if Trakt isn't configured, the movie has no credits, or cache-only mode is on and the cache misses.
- `enrich_movies(movies, has_file_only=False, watched_titles=None, watched_tmdb_ids=None, chunk_size=500, unowned_chunk_size=200, watched_chunk_size=200, cache_only=False) -> list[dict]` — returns a new list of movie dicts with a `credits` key attached where available. This is the orchestration entry point (proxied by `TraktMoviesManager.enrich_movies`).

Internal helpers worth noting:
- `_normalize(raw)` — static; converts Trakt's `cast`/`crew` (crew keyed by department, each member carrying a `jobs` list) into `{"cast": [...], "crew": [...]}`. Cast entries get `{name, id(tmdb), character(first of `characters`), order(index)}`; crew entries are flattened one-row-per-job with `{name, id(tmdb), job, department}` using `_DEPT_MAP` to humanise department keys.
- `_make_request(endpoint, fallback, _retry)` — guards on `_is_configured`, refreshes an expiring token, throttles, then GETs. Handles 429 (sleeps `Retry-After`, one retry), 401 (refresh + one retry), 404 (returns fallback), and any exception (returns fallback).
- `_throttle()` — client-side sliding-window limiter: keeps timestamps within `_RATE_WINDOW=300 s`, sleeps if `>= _RATE_LIMIT=1000` calls are in-window.
- `_advance_enrich_cursor(key, pool, size)` — reads an id-bisect cursor from global_cache, advances one `size` slice via the pure `chunk_window`, persists, returns `(start, end, ids)`. Skips the global_cache read/write when the pool is empty.
- `_advance_relevance_cursor(key, scored_rows, size)` — relevance round-robin: orders rows via `relevance_rank`, takes the next `size` not-yet-done this cycle via `relevance_window`, persists `{done_ids, size, total}`, returns `(window_ids, done_count, total)`.
- `_relevance_row(m)` — builds `(tmdbId, popularity, critic)` for the relevance sort; `critic` is TMDb rating value, falling back to IMDb.

dry_run: this manager does not gate behaviour on dry_run for fetching (FETCH/CACHE are read-side or cache-side). It propagates `dry_run` into the cache it constructs; the cache's `set` is what no-ops under dry_run.

Singleton / concurrency: `BaseManager` process-wide singleton. The persistent `requests.Session` plus the in-memory `_request_times` list back the throttle; there is no cross-process coordination — cross-process safety against the daemon comes from cache-only mode (see below), not locking.

## How it functions

Init: `super().__init__` (shared deps + auto-link to `TraktMoviesManager`) → `register()` → compute `cache_only` from `daemons.enrich.enabled` → take/build the cache → read auth from config → build the `requests.Session` and sync headers (`trakt-api-version: 2`, `trakt-api-key`, optional `Authorization: Bearer`).

Cache-only design (concurrency-critical): when the background enrichment daemon is enabled (`daemons.enrich.enabled`), the daemon owns ALL live Trakt fetching. This manager then becomes globally cache-only — `get_people` and `enrich_movies` read the daemon's cache and make ZERO live calls, so a main run can never hang on a 429. The daemon fills the cache out-of-band.

`enrich_movies` control flow:
1. **Cache-only fast path** — if `cache_only` (arg) or `self.cache_only`, attach credits purely from disk (`cache.get`) and log `attached/missing/no_tmdb_id`; no network.
2. Otherwise, build candidate sets: `all_with_tmdb`, `owned_candidates` (`hasFile`), `unowned_candidates` (empty when `has_file_only`).
3. **Decision delegated to the brain** — `priority_set`, `chunk_pool`, `chunk_window`, `relevance_rank`, `relevance_window`, and `enrich_action` come from `scripts.managers.machine_learning.acquisition.enrichment_prioritizer` (the brain; documented elsewhere, not here). These decide priority membership, ordering, the per-run window, and the per-row enrich/skip/defer action.
4. Read cache freshness ONCE for every candidate via `cache.get_fresh` into a `fresh` dict (reused in the loop — no second read); derive `fresh_ids`.
5. Advance three budgeted cursors: watched/priority (`watched_chunk_size`, id-bisect), owned (`chunk_size`, relevance), unowned (`unowned_chunk_size`, id-bisect).
6. Loop each movie: look up `(was_cached, cached)` from `fresh`, compute `selected_for_fetch`, ask `enrich_action(...)` for `"skip_no_file"` / `"enrich"` / (defer). On `"enrich"`, use the cached credits if present else `get_people(tmdb_id)`; attach under `credits`; tally `cache_hit` vs `fetched`.
7. Log a one-line summary with priority counts, fetched/cache_hit/deferred, and each cursor's progress.

Brain delegation: all selection/ordering/per-row action logic lives in `machine_learning/acquisition/enrichment_prioritizer.py` (NOT documented here per scope). This manager only executes those decisions and performs the I/O.

## Criteria & examples

- **Token refresh buffer.** `_TOKEN_BUFFER = 86_400` (1 day). If `created_at + expires_in` is `now + 3 600` (token expires in 1 hour), `_is_token_expiring()` is True (`now > expiry - 86 400`) and `_make_request` refreshes before the call.
- **Client-side throttle.** `_RATE_LIMIT = 1000` per `_RATE_WINDOW = 300 s`. With 1000 timestamps already inside the last 300 s, the next call sleeps `300 - (now - oldest) + 0.1 s` before proceeding.
- **429 handling.** A `429` with `Retry-After: 10` sleeps 10 s, then retries exactly once (`_retry=False`); a second 429 returns the fallback.
- **Owned budget per run.** `chunk_size=500`: with 1 784 owned non-priority movies, only the next 500 by relevance are fetched this run; the `chunk_cursor` records `done_ids` so subsequent runs enrich fresh slices until `done == total`, then the cycle resets.
- **Watched budget per run.** `watched_chunk_size=200`: importing a large Tautulli history yields many priority movies, but only 200 *uncached* ones are live-fetched per run (cached watched movies attach for free, uncapped) — preventing a burst of hundreds of live calls.
- **Unowned cadence.** `unowned_chunk_size=200` ≈ a full unowned cycle in ~3.5 weeks at 4 runs/day; usually empty in production because the proxy passes `has_file_only=True`.
- **No-tmdbId / no-file.** A movie with no `tmdbId` is passed through unchanged and counted `no_tmdb_id`; with `has_file_only=True`, an unowned movie returns `"skip_no_file"` and is counted `no_file`.

## In plain English

Imagine you want the full cast-and-crew list for every movie you own, pulled from an online movie database that gets annoyed (and cuts you off) if you ask too fast. This manager is the polite researcher: it checks its index-card box first (the cache), and only phones the database for cards it doesn't already have. Crucially, it doesn't try to fetch your whole collection in one sitting — each session it grabs a fixed batch (say the 500 most relevant titles), bookmarks where it stopped, and continues next time, so over a few weeks everything gets filled in without ever getting throttled. It also jumps the queue for movies you've actually watched (e.g. it'll grab *The Princess Bride*'s credits before some obscure title you own but never played). And if the standalone background helper (the enrichment daemon) is running, this researcher stops phoning out entirely and just reads whatever the helper already filed — so your main run never freezes waiting on the database.

## Interactions

- **Parent:** `TraktMoviesManager` (injects the shared cache, proxies `enrich_movies`).
- **Sibling/dependency:** `TraktMovieCacheManager` (the injected `cache_manager`) for all disk reads/writes.
- **Brain module:** `machine_learning/acquisition/enrichment_prioritizer` (`priority_set`, `chunk_pool`, `chunk_window`, `relevance_rank`, `relevance_window`, `enrich_action`) — owns the selection/ordering decisions; not documented here.
- **Downstream consumer of output:** `RadarrCacheRelationalManager.build_relations_from_movies`, which expects the exact `{"cast": [...], "crew": [...]}` credits shape produced by `_normalize`.
- **External services:** Trakt API (`api.trakt.tv`) for people + OAuth refresh; out-of-process `enrich_daemon.py` (when enabled) fills the shared cache.
