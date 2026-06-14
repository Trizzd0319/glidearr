# TraktRecommendationsManager

- **File** — `scripts/managers/services/trakt/recommendations/__init__.py`
- **One-liner** — A thin FETCH+CACHE adapter that pulls Trakt's "recommended for you" show and movie lists for the configured user and caches them.

## What it does (for a senior Python engineer)

`TraktRecommendationsManager(BaseManager, ComponentManagerMixin)` is a leaf submanager under the Trakt API manager. It exposes Trakt's personalized recommendation endpoints and persists the raw responses through `global_cache`. It makes no decisions — it only FETCHes from Trakt and CACHEs the result.

**Placement in the manager tree.** `parent_name = "TraktManager"`. It is instantiated by the Trakt API manager (`scripts/managers/services/trakt/api/__init__.py`), which loads it alongside the other Trakt submanagers (history, ratings, watchlist, lookup, analytics, universe, progress, lists, sync) and attaches it as `self.recommendations`. Note: the api manager builds these submanagers by iterating a `sub_classes` dict and calling `cls(**init_kwargs)` directly — it does NOT use `ComponentManagerMixin.load_components` for this set, even though this class mixes the mixin in. The injected kwargs include the shared `logger`/`config`/`global_cache`/`validator`/`registry`, `manager=self` (the api manager), `dry_run`, and crucially `trakt_api=self` (the api manager doubles as the HTTP client). This manager loads no submanagers of its own.

**Construction.** `__init__` sets `parent_name`, calls `super().__init__(...)` (BaseManager wiring + parent auto-link), then `self.register()`. It reads:
- `self.dry_run` — from `kwargs["dry_run"]`, falling back to the parent manager's `dry_run`, else `False`.
- `self.trakt_api` — from `kwargs["trakt_api"]`; the object whose `_make_request(...)` performs the actual HTTP calls.
- `self.user` — `config["trakt"]["username"]`, defaulting to `"default"`.

**Public methods.**
- `get_recommendations_shows(limit: int = 10) -> list` — returns recommended shows. If `global_cache` is present, delegates to `global_cache.get_or_generate_cache(key="trakt/{user}/recommendations/shows", generator_function=lambda: self._fetch_shows(limit))`; otherwise calls `_fetch_shows(limit)` directly.
- `get_recommendations_movies(limit: int = 10) -> list` — same pattern with cache key `"trakt/{user}/recommendations/movies"` and `_fetch_movies(limit)`.
- `summarize_recommendations() -> dict` — calls both getters (coalescing `None`→`[]`), logs an info line with each list's length plus a per-title debug line (`title (year)`), and returns `{"shows": [...], "movies": [...]}`.

**External API endpoints (FETCH).** `trakt_api._make_request("recommendations/shows", params={"limit": limit})` and `trakt_api._make_request("recommendations/movies", params={"limit": limit})` — the Trakt `/recommendations/shows` and `/recommendations/movies` endpoints.

**Config keys read.** `trakt.username` (default `"default"`).

**global_cache keys written/read.** `trakt/{user}/recommendations/shows` and `trakt/{user}/recommendations/movies`, where `{user}` is the configured Trakt username. No Parquet.

**dry_run behavior.** `self.dry_run` is captured but never branched on — all methods here are read-only GETs (no APPLY / PUT / DELETE / POST), so dry_run has no effect.

**Singleton / concurrency.** Inherits BaseManager's process-wide singleton behavior. Caching/concurrency is whatever `global_cache.get_or_generate_cache` provides; this class adds none of its own.

## How it functions

Lifecycle: the api manager constructs it once with the shared deps and the `trakt_api` HTTP client, the instance registers itself, and thereafter callers reach it as `trakt_api.recommendations`. There is no `run()` entry point.

Control flow on each call: a getter checks for `global_cache`; if present, it asks the cache for the keyed value and hands it a generator lambda that, on a miss/expiry, invokes the private `_fetch_shows` / `_fetch_movies`. Those helpers guard on `self.trakt_api` being truthy (return `[]` if absent), then perform the `_make_request` GET and coalesce a falsy response to `[]`. `summarize_recommendations` is a convenience that calls both getters and emits log lines.

No decision is delegated to a `machine_learning` brain module — this manager is pure I/O. The recommendations it fetches are Trakt's own server-side suggestions; any downstream value-judgement (whether to act on a recommendation) happens elsewhere.

## Criteria & examples

The only knob is `limit` (default `10`), passed straight through as the Trakt `limit` query param; there are no thresholds, scoring, or selection rules in this file.

- Worked example: `get_recommendations_shows(limit=5)` for user `"alice"` first looks up cache key `trakt/alice/recommendations/shows`. On a miss it issues `GET recommendations/shows?limit=5`; if Trakt returns 5 show dicts, those are cached and returned. If `trakt_api` were unset, `_fetch_shows` short-circuits to `[]` and the empty list is what gets cached/returned.
- Worked example: `summarize_recommendations()` with 3 shows and 0 movies logs `[TraktRec] Recommended shows: 3`, three debug lines like `  - Severance (2022)`, then `[TraktRec] Recommended movies: 0`, and returns `{"shows": [...3 dicts...], "movies": []}`.

## In plain English

Think of Trakt as a friend who has watched everything and keeps a running "you might like these next" list for you. This piece of code is just the messenger who walks over, asks "what are your top 10 show picks and top 10 movie picks for me?", writes them on a notepad (the cache) so it doesn't have to ask again right away, and reads them back. If your friend suggests The Princess Bride, the messenger simply relays "The Princess Bride (1987)" — it doesn't judge whether you'll actually like it or go add it to your library. It only fetches and remembers the list.

## Interactions

- **Parent manager:** the Trakt API manager (`scripts/managers/services/trakt/api/__init__.py`, `parent_name = "TraktManager"`), which constructs it and supplies the `trakt_api` HTTP client and shared deps. The top-level Trakt service manager (`scripts/managers/services/trakt/__init__.py`) invokes `trakt_api.recommendations.get_recommendations_shows()` / `...get_recommendations_movies()`.
- **Sibling submanagers:** history, ratings, watchlist, lookup, analytics, universe, progress, lists, sync (all built by the same api manager).
- **External services:** Trakt (`/recommendations/shows`, `/recommendations/movies`) via the api manager's `_make_request`, and `GlobalCacheManager` for persistence.
- **Brain modules:** none — this manager delegates no decisions into `machine_learning/`.
