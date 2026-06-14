# TraktLookupManager

**File** — `scripts/managers/services/trakt/lookup/__init__.py`
**One-liner** — A thin, read-only adapter exposing a catalogue of named Trakt API GET endpoints (show metadata, crew, related shows, user lists/history, engagement) as Python methods, each of which simply forwards to the shared `trakt_api._make_request`.

## What it does (for a senior Python engineer)

`TraktLookupManager(BaseManager, ComponentManagerMixin)` is one of ten Trakt sub-managers. It is the lookup/read surface of the Trakt service: a flat collection of convenience methods, each wrapping a single Trakt API `GET` path. It holds no state of its own beyond `dry_run` and a reference to the Trakt API client (`trakt_api`).

It performs only **FETCH** — every public method ultimately calls the private `_get(endpoint, params)` helper, which delegates to `self.trakt_api._make_request(endpoint, params=params)` (HTTP GET against `https://api.trakt.tv`). It does **no CACHE** of its own (it returns the raw decoded JSON to the caller; persistence is the caller's concern) and **no APPLY** (no PUT/POST/DELETE — nothing is mutated on Trakt or locally).

Public methods, grouped as in the source:

- **ID lookups**
  - `lookup_trakt_id_from_tvdb(tvdb_id)` → GET `search/tvdb/{tvdb_id}`. Resolves a TVDB id to Trakt search results.
  - `get_metadata_from_tvdb(tvdb_id)` → GET `search/tvdb/{tvdb_id}?type=show`. Same, narrowed to shows.
  - `search_show_by_title_and_year(title, year=None)` → GET `search/show` with params `{"query": title}`, plus `"year"` when supplied.
- **Show lookups**
  - `get_aliases_by_show(show_id)` → GET `shows/{show_id}/aliases`.
  - `get_seasons_by_show(show_id)` → GET `shows/{show_id}/seasons`.
  - `get_episodes_by_season(show_id, season_number: int)` → GET `shows/{show_id}/seasons/{season_number}`.
  - `get_related_shows(show_id)` → GET `shows/{show_id}/related`.
  - `get_trending_shows()` → GET `shows/trending`.
  - `get_popular_shows()` → GET `shows/popular`.
  - `get_anticipated_shows()` → GET `shows/anticipated`.
- **People / crew lookups**
  - `lookup_people_by_show(show_id)` → GET `shows/{show_id}/people` (returns the full cast+crew payload).
  - `lookup_directors_by_show(show_id)` → calls `lookup_people_by_show`, then filters `crew.directing` to entries whose `job == "Director"`.
  - `lookup_producers_by_show(show_id)` → filters `crew.production` for `job == "Producer"`.
  - `lookup_composers_by_show(show_id)` → filters `crew.sound` for `job == "Composer"`.
  - `get_writers_by_show(show_id)` → returns the entire `crew.writing` list unfiltered.
- **User lookups**
  - `get_user_lists(username)` → GET `users/{username}/lists`.
  - `get_list_items(username, list_slug)` → GET `users/{username}/lists/{list_slug}/items`.
  - `get_user_collections(username)` → GET `users/{username}/collection/shows`.
  - `get_user_followers(username)` → GET `users/{username}/followers`.
  - `get_user_following(username)` → GET `users/{username}/following`.
  - `get_user_watch_history(username)` → GET `users/{username}/history`.
  - `get_user_ratings(username)` → GET `users/{username}/ratings`.
- **Engagement**
  - `get_watchers_by_show(show_id)` → GET `shows/{show_id}/watching` (who is currently watching).
  - `get_comments_by_show(show_id)` → GET `shows/{show_id}/comments`.
- **Private**
  - `_get(endpoint, params=None)` → returns `None` immediately if `self.trakt_api` is falsy; otherwise `self.trakt_api._make_request(endpoint, params=params)`.

Position in the manager tree: it declares `parent_name = "TraktManager"`, but it is actually constructed by the **TraktAPI** manager (`scripts/managers/services/trakt/api/__init__.py`). TraktAPI instantiates it directly via `setattr(self, "lookup", TraktLookupManager(**init_kwargs))` inside a `sub_classes` dict — it does **not** go through `ComponentManagerMixin.load_components`, even though the class mixes that in. So at runtime `trakt_api.lookup` is this manager, and its injected `trakt_api=self` is the TraktAPI instance. (`TraktManager` itself logs "No components to pre-load at this time," so the lookup manager is not a direct child of TraktManager despite the `parent_name`.)

Config keys read: **none directly.** All credentials, base URL, token refresh and rate-limit config are owned by TraktAPI; this manager never touches `self.config`.

global_cache / Parquet keys: **none read or written here.** This class returns live JSON; callers (e.g. the show/movie scorers, universe and recommendation managers) are responsible for any caching.

External API endpoints touched: all the `https://api.trakt.tv/...` paths listed above, reached through `trakt_api._make_request`.

dry_run behavior: `self.dry_run` is captured (from `kwargs["dry_run"]`, else the parent's `dry_run`, else `False`), but it is **never consulted** — every method here is a read-only GET, so there is nothing to suppress. dry_run has no effect on this manager.

Singleton / concurrency / threading: as a `BaseManager` it is a process-wide singleton keyed by `(class, singleton_key)`. It is thread-safe to call concurrently because it carries no mutable per-request state; the actual rate-limiting, token-refresh locking and request throttling all live in `TraktAPI._make_request` / `_throttle` (which holds `_throttle_lock`), not here.

## How it functions

Lifecycle:
1. `__init__` (wrapped by `LoggerManager().log_function_entry` and `@timeit("__init__")`) sets `self.parent_name = "TraktManager"`, calls `super().__init__(...)` (BaseManager wires in logger/config/global_cache/validator/registry and auto-links to the parent), then `self.register()`.
2. It reads three things from `kwargs`: the `manager` (parent) reference, `dry_run` (falling back to the parent's), and `trakt_api` (the HTTP client it will delegate to).
3. There is **no** `load_components` call and **no** run/entry method — this manager has no orchestration phase. It is purely a passive lookup library invoked on demand by other Trakt sub-managers.

Main control flow per call: a public method builds an endpoint string (and optional `params` dict), hands it to `_get`, which guards on `trakt_api` being present and then calls `trakt_api._make_request`. The crew helpers (`lookup_directors_by_show`, etc.) add one layer: they fetch the full `shows/{id}/people` payload once and then do a pure in-Python filter (`[p for p in ... if p.get("job") == "..."]`), defensively coalescing missing keys with `(crew or {}).get(...)`.

Decisions delegated to a machine_learning brain module: **none.** This manager makes no value judgements; it is pure data retrieval. (Out of scope per task: it does not import or call anything under `machine_learning/`.)

## Criteria & examples

The only "rules" here are tiny input/output guards and the crew filters:

- **trakt_api guard.** `_get` returns `None` when `self.trakt_api` is falsy. Example: if TraktAPI failed to construct and injected `trakt_api=None`, then `get_seasons_by_show(190430)` returns `None` instead of raising — the caller sees an empty result and skips gracefully.
- **Optional `year` param.** `search_show_by_title_and_year("Dexter")` sends `?query=Dexter`; `search_show_by_title_and_year("Dexter", 2006)` sends `?query=Dexter&year=2006`, narrowing the search to the 2006 original rather than the 2021 revival.
- **Director filter.** Given `shows/{id}/people` whose `crew.directing` list contains one entry with `job == "Director"` and another with `job == "Co-Director"`, `lookup_directors_by_show` returns only the first — the `Co-Director` is dropped because the comparison is an exact `== "Director"`.
- **Writers are unfiltered.** Unlike directors/producers/composers, `get_writers_by_show` returns the entire `crew.writing` list verbatim — every "Writer", "Story", "Screenplay", etc. entry is included, with no `job` filter applied.
- **Defensive coalescing.** If `lookup_people_by_show` returns `None` (e.g. trakt_api missing) or a payload with no `crew` key, the crew helpers still return `[]` rather than raising, because each step uses `(crew or {}).get("crew", {}).get(...)` with `[]` defaults.

## In plain English

Think of this manager as the front-desk reference librarian for everything Glidearr wants to know about a TV show from Trakt. You don't fetch the book yourself; you walk up and ask a specific question — "Who directed *Stranger Things*?", "What shows are similar to *The Office*?", "What's trending right now?", "Show me this user's watch history" — and the librarian goes to the exact shelf (a specific Trakt web address) and brings back the answer.

It only ever *reads* and *reports*. It never changes anything on Trakt and never decides what to do with the information — that's someone else's job. If the library is closed (the Trakt connection isn't set up), the librarian just shrugs and hands back nothing rather than causing a scene. And for a question like "who directed this?", it grabs the whole credits page and then quietly hands you back only the people whose title is literally "Director."

## Interactions

- **Parent / owner:** Declares `parent_name = "TraktManager"`, but is actually created and held by **TraktAPI** (`trakt_api.lookup`). It delegates every HTTP call to that same TraktAPI instance via `trakt_api._make_request`, which owns auth, token refresh, rate limiting (1000 requests / 300s window) and the stale-cache 429 fallback.
- **Sibling sub-managers** (also created by TraktAPI): `history` (TraktHistoryManager), `ratings` (TraktRatingsManager), `recommendations` (TraktRecommendationsManager), `watchlist` (TraktWatchlistManager), `analytics` (TraktAnalyticsManager), `universe` (TraktUniverseManager), `progress` (TraktProgressManager), `lists` (TraktListsManager), `sync` (TraktSyncManager). Those managers are the typical callers of these lookup methods (e.g. for related-show graphs, crew enrichment, search-by-title resolution).
- **Brain modules / other services:** none. This manager talks to no `machine_learning/` module and to no service other than TraktAPI. The crew/related/metadata it returns may later feed brain features (e.g. related-graph affinity scoring), but that consumption happens in the callers, not here.

Note: `scripts/managers/services/trakt/lookup.py` is a deprecated shim that re-exports `TraktLookupManager` from this package; new code should import from the `lookup` package.
