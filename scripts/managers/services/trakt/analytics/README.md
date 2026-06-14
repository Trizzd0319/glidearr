# TraktAnalyticsManager

- **File** — `scripts/managers/services/trakt/analytics/__init__.py`
- **One-liner** — A thin, read-only Trakt sub-manager that fetches raw user stats and tallies the household's most-watched genres and actors from Trakt watch history.

## What it does (for a senior Python engineer)

`TraktAnalyticsManager(BaseManager, ComponentManagerMixin)` is a leaf service sub-manager under the Trakt API tree. Its whole job is to read data from the Trakt API — it is a pure **FETCH** adapter. It never CACHEs (no `global_cache` / Parquet writes anywhere in the file) and never APPLYs (no PUT/POST/DELETE; every call routes through the parent's GET helper). Despite mixing in `ComponentManagerMixin`, it does **not** call `load_components`, so it loads no submanagers of its own.

Position in the manager tree: `parent_name = "TraktManager"` (set both as a class attribute and again in `__init__`). In practice it is instantiated as the `analytics` component of `TraktAPIManager` (`scripts/managers/services/trakt/api/__init__.py`, line 98), which constructs all Trakt sub-managers in a loop and injects `trakt_api=self`. So the live parent/owner is `TraktAPIManager`; all HTTP goes back through it.

Constructor — `__init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs)`:
- Calls `super().__init__(...)` (BaseManager wiring: shared logger/config/cache/validator/registry, auto-link to parent) then `self.register()`.
- `self.dry_run` is read from `kwargs["dry_run"]`, falling back to the parent manager's `dry_run`, else `False`. (Note: `dry_run` is captured but never used — this manager only reads, so there is nothing to gate.)
- `self.trakt_api = kwargs.get("trakt_api")` — the shared `TraktAPIManager` used for every request.

Public methods:
- `fetch_user_stats()` — GET `users/stats`; returns the parsed Trakt response (or `None` if no API).
- `fetch_user_history_summary()` — GET `sync/history`.
- `fetch_user_watchlist_summary()` — GET `sync/watchlist`.
- `analyze_genres() -> dict` — walks the household's watched series and tallies genre frequency. Returns an ordered dict of `{genre: count}`, highest count first.
- `analyze_actors() -> dict` — walks the same watched series and tallies cast-member appearance frequency. Returns an ordered dict of `{actor_name: count}`, highest count first.

Private helpers:
- `_get(endpoint, params=None)` — guards on `self.trakt_api`; delegates to `self.trakt_api._make_request(endpoint, params=params)` (default method GET). Returns `None` when there is no API.
- `_history_manager()` — returns the sibling `TraktHistoryManager` instance via `self.trakt_api.history` (the API manager registers all siblings as attributes), or `None` if unavailable.

External Trakt API endpoints touched (all GET, via the parent's authenticated, rate-limited `_make_request`):
- `users/stats`
- `sync/history`
- `sync/watchlist`
- `search/tvdb/{tvdb_id}?type=show` (per watched series, to read `show.genres`)
- `search/tvdb/{tvdb_id}/people` (per watched series, to read `cast`)

Config keys read: none directly (Trakt auth/credentials live on `TraktAPIManager`). `global_cache` / Parquet keys read or written: none. dry_run behavior: none (read-only manager; the flag is stored but inert). Concurrency/threading: no locks here; the parent `_make_request`/`_throttle` provides the rate-limit lock, so concurrent calls are serialized upstream. BaseManager makes the instance a process-wide singleton keyed by `(class, singleton_key)`.

## How it functions

Lifecycle: `__init__` → BaseManager dependency injection + `register()` → store `dry_run` and `trakt_api`. There is no `load_components` step and no long-running `run` entry point — callers invoke the individual `fetch_*` / `analyze_*` methods on demand.

`analyze_genres` control flow:
1. Resolve the sibling history manager via `_history_manager()`; if absent, return an empty dict immediately.
2. `history.get_history_grouped_by_series()` returns a dict keyed by **TVDB id** (built from full watch history, grouping episodes per show — see `scripts/managers/services/trakt/history/__init__.py`).
3. For each `tvdb_id`, GET `search/tvdb/{tvdb_id}?type=show`. The response is a list; take `[0]["show"]["genres"]` and increment each genre's count.
4. Sort by count descending, log each at debug, and return the sorted dict.

`analyze_actors` is structurally identical but hits `search/tvdb/{tvdb_id}/people`, iterates `people["cast"]`, and counts `cast[i]["person"]["name"]`.

No decision is delegated to any `machine_learning` brain module — this manager only produces raw counts/summaries. (Downstream affinity scoring and genre weighting live elsewhere in the system; this class does not make value judgements.)

## Criteria & examples

The selection rules here are minimal and defensive rather than threshold-based:
- **History gate**: if `_history_manager()` returns `None` (Trakt history sub-manager missing/unconfigured), both analyze methods short-circuit to an empty dict — no API calls are made.
- **Per-item guards**: `analyze_genres` only counts a result when `metadata` is a non-empty list (`isinstance(metadata, list) and metadata`); otherwise that series is skipped. `analyze_actors` skips a series when `people` is falsy and skips any cast entry lacking a `person.name`.
- **Ranking**: results are sorted by count descending with `sorted(..., key=lambda x: x[1], reverse=True)`; there is no top-N cutoff — every genre/actor seen is returned.

Worked example: suppose the household has watched 4 shows whose TVDB lookups report genres — *The Mandalorian* `["Science Fiction","Action","Adventure"]`, *Andor* `["Science Fiction","Drama"]`, *Loki* `["Science Fiction","Fantasy"]`, *Bluey* `["Animation","Family"]`. `analyze_genres()` returns `{"Science Fiction": 3, "Action": 1, "Adventure": 1, "Drama": 1, "Fantasy": 1, "Animation": 1, "Family": 1}` — "Science Fiction" leads with 3 because it appeared in three of the four shows. For `analyze_actors()`, if Pedro Pascal appears in the cast of both *The Mandalorian* and a second watched show, his name ends up with count 2 and ranks above one-appearance actors.

## In plain English

Think of this as the person at a video store who keeps a tally sheet of everything your family has watched. Every time you finish a show, they look up what kind of show it was and who was in it, and add a tick mark — "Sci-Fi: another one," "Pedro Pascal: seen him again." After going through your whole watch history, they hand you a ranked list: "Your house watches a LOT of sci-fi, and you keep coming back to anything with Pedro Pascal in it." It does not decide what to record next or delete anything — it just counts what already happened so the rest of the system understands your taste.

## Interactions

- **Parent manager**: `TraktAPIManager` (`scripts/managers/services/trakt/api/__init__.py`) — owns the authenticated session, token refresh, and rate-limited `_make_request`; constructs this class as its `analytics` attribute and injects `trakt_api=self`. (`parent_name` is declared as `"TraktManager"` for the BaseManager registry link.)
- **Sibling submanagers**: `TraktHistoryManager` (the `history` sibling) supplies `get_history_grouped_by_series()`, the source of the TVDB ids this manager iterates. Other Trakt siblings (`ratings`, `recommendations`, `watchlist`, `lookup`, `universe`, `progress`, `lists`, `sync`) are peers under the same parent but are not used here.
- **External services**: the Trakt API only (via the parent's session).
- **Brain modules**: none — this manager delegates no decisions into `machine_learning/`.

Note: `scripts/managers/services/trakt/analytics.py` is a deprecated shim that re-exports `TraktAnalyticsManager` from this package; new code should import from the package.
