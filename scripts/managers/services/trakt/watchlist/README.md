# TraktWatchlistManager

- **File** — `scripts/managers/services/trakt/watchlist/__init__.py`
- **One-liner** — A thin FETCH/CACHE adapter that retrieves the signed-in Trakt user's "want to watch" lists (shows and movies) and caches them under a per-user key.

## What it does (for a senior Python engineer)

`TraktWatchlistManager(BaseManager, ComponentManagerMixin)` is one of the Trakt API sub-managers. Its sole job is to read the authenticated user's Trakt **watchlist** (the explicit "I want to watch this" backlog the user curates on trakt.tv) for both media types and hand the raw response back, cached.

**Position in the manager tree.** It declares `parent_name = "TraktManager"`, so `BaseManager` auto-links it to `TraktManager` for inherited logger/config/cache/validator. In practice, however, it is **constructed and owned by `TraktAPIManager`** (`scripts/managers/services/trakt/api/__init__.py`), which instantiates it in its sub-manager loop, injects `trakt_api=self`, and attaches it as `self.trakt_api.watchlist`. It loads **no** submanagers itself — although it mixes in `ComponentManagerMixin`, it never calls `load_components`.

**Public methods.**
- `get_watchlist_shows(force_refresh: bool = False) -> list` — returns the cached list of watchlisted shows (raw Trakt list items). When `force_refresh=True` and a `global_cache` is present, it first invalidates the cache key so the fetch re-runs; otherwise it serves the cache or generates it. Falls back to a direct fetch if there is no `global_cache`.
- `get_watchlist_movies(force_refresh: bool = False) -> list` — same contract for watchlisted movies.

**Private helpers.**
- `_fetch_watchlist_shows() -> list` — the cache generator for shows; no-ops to `[]` if `trakt_api` is missing.
- `_fetch_watchlist_movies() -> list` — the cache generator for movies; same guard.

**FETCH / CACHE / APPLY.** FETCH and CACHE only. There is no APPLY path — this manager never PUT/POST/DELETEs to Trakt, so `dry_run` is captured (from kwarg or parent) but has no effect on its behavior.

**External API endpoints (via `trakt_api._make_request`).**
- `GET users/me/watchlist/shows` with `params={"page": 1, "limit": 100}`
- `GET users/me/watchlist/movies` with `params={"page": 1, "limit": 100}`

Note: `users/me/...` resolves to whoever the injected OAuth bearer token belongs to; the `username` from config is used only to namespace the cache key, not the endpoint.

**Config keys read.**
- `trakt.username` (defaults to `"default"`) — used to build the cache key namespace (`self.user`).

**global_cache keys read/written.**
- `trakt/<user>/watchlist/shows`
- `trakt/<user>/watchlist/movies`

Both are read via `global_cache.get_or_generate_cache(...)`, written by the matching `_fetch_*` generator, and invalidated via `global_cache.invalidate_cache_key(...)` when `force_refresh=True`.

**Singleton / concurrency.** As a `BaseManager`, instances are cached process-wide in `_instances` keyed by `(class, singleton_key)`. No threading or locks of its own. `__init__` is wrapped by `LoggerManager().log_function_entry` and `@timeit`.

## How it functions

Lifecycle: `__init__` calls `super().__init__` (which injects the shared deps and auto-links the parent), then `self.register()`. It reads `dry_run` and the injected `trakt_api` from kwargs, and resolves `self.user` from `trakt.username`. There is no `prepare`/`run` entry point — the manager is invoked on demand by its parent's `run()` (see `TraktManager.run`, which calls `self.trakt_api.watchlist.get_watchlist_shows()`).

Control flow per public call: build the per-user cache key → if `force_refresh`, invalidate it → delegate to `get_or_generate_cache`, which serves the cached list or runs the `_fetch_*` generator on a miss. The generator calls `trakt_api._make_request` (the shared HTTP layer that owns the bearer token, rate-limiting, and retries) for page 1, capped at 100 items, logs an info line with the count (or a warning on empty/failed retrieval), and returns `data or []`.

No decision is delegated to any `machine_learning` brain module; this manager only fetches and caches. Downstream consumers (e.g. recommendation/affinity scoring) read these cached lists elsewhere.

## Criteria & examples

The only hard rules in the code:
- **Page/limit cap:** every fetch requests `page=1, limit=100`. There is no pagination loop, so a watchlist with 137 entries returns **only the first 100**; the remaining 37 are silently dropped.
- **`force_refresh` gate:** invalidation only happens when `force_refresh=True` **and** `global_cache` is present. Example: a user adds *The Princess Bride* to their Trakt watchlist; a normal `get_watchlist_movies()` call still returns the previously cached list (without the new entry) until the cache expires or someone calls `get_watchlist_movies(force_refresh=True)`, which invalidates `trakt/<user>/watchlist/movies` and re-fetches.
- **Missing-API guard:** if `self.trakt_api` is falsy (not injected), `_fetch_watchlist_shows`/`_fetch_watchlist_movies` return `[]` immediately rather than raising.
- **Empty/failed handling:** when `_make_request` returns a falsy value, the manager logs `"[TraktWatchlist] Empty or failed ... retrieval."` and returns `[]`.

## In plain English

Think of your Trakt watchlist as the sticky-note list on your fridge of movies and shows you've told yourself "I really want to watch this" — say you jotted down *The Princess Bride* and the next Marvel film. This little helper is the errand-runner that goes to Trakt, reads that list, and brings it back so the rest of the app knows what you're actually hoping to see. To be quick about it, it keeps a photocopy (the cache) and reuses that copy instead of pestering Trakt every time — unless you explicitly say "go check again, my list changed." It only reads the list; it never adds to or crosses anything off your fridge. The catch worth knowing: it only grabs the first 100 items, so if your wishlist is enormous, the tail end won't be picked up.

## Interactions

- **Parent (owner):** `TraktAPIManager` (`scripts/managers/services/trakt/api/__init__.py`) constructs it, injects `trakt_api=self`, and exposes it as `self.trakt_api.watchlist`. `parent_name` nominally points at `TraktManager`, which is the grandparent and the one whose `run()` triggers `get_watchlist_shows()`.
- **Sibling sub-managers (under `TraktAPIManager`):** `history`, `ratings`, `recommendations`, `lookup`, `analytics`, `universe`, `progress`, `lists`, `sync`.
- **Services it talks to:** the Trakt HTTP layer via `trakt_api._make_request`; the shared `GlobalCacheManager` for read/generate/invalidate.
- **Brain modules:** none — this manager delegates no decisions to `machine_learning/`.
