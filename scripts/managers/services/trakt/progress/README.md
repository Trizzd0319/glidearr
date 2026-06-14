# TraktProgressManager

- **File** — `scripts/managers/services/trakt/progress/__init__.py`
- **One-liner** — A thin Trakt submanager that fetches per-show watched/collected episode progress, bulk-fetches it concurrently for the whole watched list/collection, and caches the combined result for 24 hours.

## What it does (for a senior Python engineer)

`TraktProgressManager(BaseManager, ComponentManagerMixin)` is a leaf service adapter under `TraktManager` (declared via `parent_name = "TraktManager"`). Its job is purely FETCH + CACHE — it reads Trakt show-progress endpoints and persists the combined results into `global_cache`. It makes no PUT/DELETE/POST calls and renders no value judgements, so it does not delegate to any `machine_learning` brain module.

**Init dependencies.** Standard `BaseManager` signature `(logger, config, global_cache, validator, registry, **kwargs)`; it calls `super().__init__(...)` then `self.register()`. From `kwargs` it pulls:
- `manager` — the parent (`TraktManager`), used only to inherit `dry_run`.
- `dry_run` — `kwargs["dry_run"]`, else parent's `dry_run`, else `False`.
- `trakt_api` — the `TraktAPIManager` instance (`trakt_api` per service-specific naming); all HTTP goes through it.

It also reads `self.config.get("trakt", {})["username"]` into `self.user` (default `"default"`); `self.user` is the only segment of the cache keys that varies per user.

**Submanagers.** None. Although it mixes in `ComponentManagerMixin`, it never calls `load_components`, so it has no child components.

**Public methods.**
- `get_progress_watched(show_id)` — single FETCH of `shows/{show_id}/progress/watched` with params `hidden=false, specials=false, count_specials=true`. Returns the raw Trakt JSON (or `None` if no api).
- `get_progress_collected(show_id)` — same for `shows/{show_id}/progress/collected`.
- `get_combined_progress_watched() -> dict` — returns watched progress for *every* show in the user's Trakt watched list, keyed by show slug. Backed by `global_cache.get_or_generate_cache(key="trakt/{user}/progress/watched_combined", generator=_fetch_combined_progress_watched, expiration_time=86_400)`. Falls back to a direct (uncached) fetch if `global_cache` is absent.
- `get_combined_progress_collected() -> dict` — same shape for the user's collection; cache key `trakt/{user}/progress/collected_combined`, same 24h TTL.
- `invalidate_progress_cache() -> None` — calls `global_cache.invalidate_cache_key("trakt/{user}/progress/watched_combined")` so the next combined-watched call re-fetches. Note: it invalidates only the *watched* key, not the *collected* key.
- `get_recent_watched_show_ids(days=30) -> set` — FETCHes `sync/watched/shows`, then returns the set of show slugs whose `last_watched_at` is newer than `now - days`. Not cached itself (though it goes through the api layer, which may throttle/cache there).

**FETCH / CACHE / APPLY.** FETCH (Trakt GETs) and CACHE (the two `*_combined` Parquet/global_cache keys). No APPLY.

**External API endpoints touched** (all GET, via `trakt_api._make_request`):
- `shows/{show_id}/progress/watched`
- `shows/{show_id}/progress/collected`
- `sync/watched/shows`
- `sync/collection/shows`

**Config keys read.** `trakt.username` (default `"default"`).

**global_cache keys read/written.**
- `trakt/{user}/progress/watched_combined` (read+write via `get_or_generate_cache`; invalidated by `invalidate_progress_cache`)
- `trakt/{user}/progress/collected_combined` (read+write via `get_or_generate_cache`)

**dry_run behavior.** `self.dry_run` is captured but never consulted — the class is read-only, so a dry run and a live run behave identically. There is nothing to "would …".

**Singleton / concurrency / threading notes.** As a `BaseManager` it is a process-wide singleton. The bulk fetch (`_fetch_combined_progress`) uses a `ThreadPoolExecutor(max_workers=5)` to issue the ~150 independent per-show GETs in parallel. The module relies on `TraktAPIManager._throttle()` to enforce Trakt rate limiting centrally and thread-safely — this class does no locking of its own. Per-show failures are caught and logged as warnings; the show is simply dropped from the result dict rather than aborting the batch.

## How it functions

Lifecycle: `__init__` → `super().__init__` (BaseManager dep injection + parent auto-link) → `register()` → capture `manager`/`dry_run`/`trakt_api`/`user`. There is no `run()` entry point; it is a passive helper invoked on demand by the parent `TraktManager` (and downstream watched-set/scoring consumers).

Main control flow for the bulk paths:
1. `get_combined_progress_watched()` / `get_combined_progress_collected()` ask `global_cache.get_or_generate_cache(...)`. On a cache hit within 24h, the cached dict is returned; on a miss/expiry, the generator runs.
2. The generator (`_fetch_combined_progress_watched` / `_collected`) first lists the relevant shows (`_get_user_watched_shows()` → `sync/watched/shows`, or `_get_collected_shows()` → `sync/collection/shows`), then hands that list plus the per-show getter to `_fetch_combined_progress`.
3. `_fetch_combined_progress(shows, getter)` extracts each show's `show.ids.slug`, drops falsy slugs, submits one `getter(sid)` task per slug to the 5-worker pool, and assembles `{slug: progress_json}` as futures complete. It logs a one-line count (e.g. `Fetched watched progress for 142 shows.`).

Notable internal helpers: `_get(endpoint, params)` is the single choke point to `trakt_api._make_request` and short-circuits to `None` when `trakt_api` is missing; `_get_user_watched_shows()` / `_get_collected_shows()` wrap the two sync-list endpoints with an `or []` guard.

No decision is delegated to `machine_learning` — this is a pure data adapter.

## Criteria & examples

- **24-hour cache TTL** (`_PROGRESS_TTL = 86_400`): if `get_combined_progress_watched()` was last generated 10 hours ago, it returns the cached dict and fires zero HTTP calls; at 25 hours the cache has expired and the ~150-call concurrent fetch runs again. Calling `invalidate_progress_cache()` first forces an immediate re-fetch regardless of age.
- **Concurrency cap = 5**: for a user with 150 watched shows, work is dispatched 5 GETs at a time through the pool (rate-limited centrally by the api layer) rather than 150 sequential blocking calls.
- **Recency filter in `get_recent_watched_show_ids(days=30)`**: a show with `last_watched_at = 2026-05-20` evaluated on 2026-06-10 has a cutoff of 2026-05-11, so `2026-05-20 > 2026-05-11` → its slug is included. A show last watched `2026-04-01` is older than the cutoff and is excluded. Timestamps that don't parse against `"%Y-%m-%dT%H:%M:%S.%fZ"` are silently skipped (the `except ValueError: pass`), as are items missing either `last_watched_at` or `show.ids.slug`.
- **Per-show failure tolerance**: if 3 of 142 progress GETs raise, those 3 slugs are logged as warnings and omitted; the returned dict contains the 139 that succeeded.

## In plain English

Think of your Trakt account as a bookmark in every TV series you watch. This component's job is to go ask Trakt, for each show, "how far along is this person?" — e.g. for *The Mandalorian* it learns you've finished Season 2 but haven't started Season 3, and for *Bluey* it sees you've watched 40 of 51 episodes. Because asking about every single show one at a time would be slow (imagine phoning a library 150 times in a row), it sends five questions at once and then writes the whole answer sheet down for a full day, so it doesn't have to keep re-asking. It also has a quick "what have I watched lately?" lookup that returns just the shows you've touched in the last 30 days. It only ever *reads* this information — it never changes anything on your Trakt account — which is why it behaves the same whether or not the app is in safe "dry run" mode.

## Interactions

- **Parent manager:** `TraktManager` (auto-linked via `parent_name`; shares its logger/config/global_cache/validator/registry and inherits `dry_run`).
- **Sibling submanagers:** none loaded by this class; it sits alongside other Trakt submanagers (e.g. the `movies/`, `api/` packages) under `TraktManager`.
- **Services it talks to:** `TraktAPIManager` (`trakt_api`) for all HTTP and rate-limiting (`_make_request`, `_throttle`); `GlobalCacheManager` (`global_cache`) for the two 24h combined-progress caches.
- **Brain modules:** none — pure FETCH/CACHE adapter; no `machine_learning` delegation.
- **Downstream consumers:** the combined watched/collected progress and recent-watched slug set feed the broader watched-set and TV scoring logic elsewhere in the app.
