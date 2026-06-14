# TraktManager

**File** — `scripts/managers/services/trakt/__init__.py`
**One-liner** — Top-level Trakt service manager: validates Trakt credentials, stands up the Trakt HTTP/API layer, and on `run()` drives a system-wide pull of watch history, ratings, recommendations, watchlist, and progress.

## What it does (for a senior Python engineer)

`TraktManager(BaseManager, ComponentManagerMixin)` is one of the top-level service managers constructed by `Main` in `scripts/main.py` (in the same sequence as Sonarr/Radarr/Tautulli). Like all managers it is a process-wide singleton via `BaseManager`, sharing the injected logger / config (`ConfigManager`) / global_cache (`GlobalCacheManager`) / validator / registry with its parent.

It is the root of the Trakt manager subtree. It does **not** use `load_components` directly; instead it wires up two child managers by hand in `__init__`:

- `self.instance_manager = TraktInstanceManager(**base_kwargs)` — the credential/token validator (`trakt_api` instance manager). Construction is immediately followed by `register_and_validate()`; if that returns falsy, `__init__` raises `RuntimeError("[TraktManager] Instance validation failed — aborting setup.")`, so an unauthenticated Trakt aborts the whole Trakt setup rather than running degraded.
- `self.trakt_api = self._singleton("trakt_api_manager", lambda: TraktAPIManager(**base_kwargs))` — the HTTP layer plus all the Trakt sub-managers (history, ratings, recommendations, watchlist, progress, lookup, universe, analytics, lists, movies, shows, sync, ...). `_singleton` (from `BaseManager`) caches the instance under the name `"trakt_api_manager"`.

Both children receive the same `base_kwargs` dict: `logger`, `config`, `global_cache`, `validator`, `registry`, `manager=self`, and `dry_run` — so they auto-link back to this manager as parent and inherit the shared dependency set.

**Optional cross-service handles.** `__init__` stashes (but never instantiates) handles a caller may inject so the Trakt subtree can reach sibling services without owning them:
- `self.sonarr_apis = kwargs.get("sonarr_apis", {})`
- `self.ml_manager = kwargs.get("ml_manager")`
- `self.tautulli_api = kwargs.get("tautulli_api")`
- `self.plex_api = kwargs.get("plex_api")`

**FETCH / CACHE / APPLY role.** This class itself is a thin orchestrator — it performs no HTTP or cache I/O of its own. The actual FETCH (GET) and CACHE work happens inside the `trakt_api` sub-managers it calls in `run()`. There is no APPLY (PUT/POST/DELETE) issued directly from this class.

**Config keys read.** None read directly here. `dry_run` is taken from `kwargs["dry_run"]`, falling back to the parent manager's `dry_run`, else `False`. (The `ConfigManager` and the actual `dry_run` config value are resolved upstream in `Main`.)

**global_cache / Parquet keys.** None written or read directly by this class; the sub-managers invoked in `run()` own their own cache keys (e.g. watch-history, ratings, recommendations, watchlist, and combined-progress caches).

**dry_run behavior.** Captured and forwarded to children via `base_kwargs`. This class issues no writes itself, so it has no "would …" lines of its own; dry-run gating lives in the leaf managers that perform APPLY/auto-rating.

**Singleton / concurrency notes.** Standard `BaseManager` singleton. `trakt_api` is additionally guarded by `_singleton("trakt_api_manager", …)`. No threading is introduced here.

## How it functions

Lifecycle:

1. **`__init__(logger, config, global_cache, validator, registry, **kwargs)`** — sets `parent_name = "TraktManager"`, calls `super().__init__`, then `self.register()`. Resolves `dry_run`, captures optional cross-service handles, builds `base_kwargs`. Constructs `TraktInstanceManager` and validates it (`register_and_validate()` — abort on failure). Constructs/caches `TraktAPIManager` as `self.trakt_api`. Logs `"[TraktManager] Initialized successfully."`.
2. **`prepare(self)`** — a no-op stub: logs `"[TraktManager] No components to pre-load at this time."`. (Present to satisfy the manager lifecycle contract; there is nothing to warm up.)
3. **`run(self)`** — the system-wide sync entry point. In order it calls, via `self.trakt_api`:
   - `history.get_full_watch_history()`
   - `history.get_full_movie_history_cached()`
   - `ratings.get_user_ratings()`
   - `recommendations.get_recommendations_shows()`
   - `recommendations.get_recommendations_movies()`
   - `watchlist.get_watchlist_shows()`
   - `progress.get_combined_progress_watched()` → stored in local `progress`
   - `ratings.auto_rate_watched_shows(progress_map=progress)`

   Then logs `"[TraktManager] System sync complete."`.

A notable optimization: progress is fetched once and the resulting map is handed straight to `auto_rate_watched_shows(progress_map=progress)`. The inline comment notes this avoids a second round of ~150 per-show API calls that `auto_rate_watched_shows` would otherwise make internally.

**Decisions delegated to a brain.** This class makes no value-judgements itself. Any auto-rating decision logic and any ML hook reachable through `self.ml_manager` lives under `machine_learning/` (not documented here). `TraktManager` only sequences the FETCH/CACHE calls and passes data along.

Both `__init__` and `run`/`prepare` are wrapped with `@LoggerManager().log_function_entry` and `@timeit(...)` for structured entry logging and timing.

## Criteria & examples

This class applies exactly one hard guard of its own:

- **Credential gate.** After constructing `TraktInstanceManager`, if `register_and_validate()` is falsy it raises `RuntimeError`. Worked example: a user with an expired or never-configured Trakt OAuth token → `register_and_validate()` returns `False` → `TraktManager.__init__` raises and the Trakt subtree never comes up, instead of silently producing empty history/ratings caches downstream.

- **dry_run resolution order.** `dry_run = kwargs.get("dry_run", parent.dry_run if parent else False)`. Worked example: parent (`Main`) constructed with `dry_run=True` and `TraktManager(manager=parent)` called without an explicit `dry_run` kwarg → `self.dry_run` becomes `True`, and that `True` is propagated into `base_kwargs` to both children.

All thresholds (watched-set rules, auto-rate eligibility, recommendation/likelihood scoring, etc.) live in the leaf sub-managers and the ML brain, not here.

## In plain English

Think of `TraktManager` as the manager on duty for everything Trakt-related. The first thing it does is check your "ID badge" — your Trakt login. If the badge doesn't scan, it shuts the Trakt counter down entirely rather than pretending to work. Once you're in, it doesn't do the lifting itself: it hands the work to specialist clerks (history, ratings, recommendations, watchlist, progress) and tells them, in order, to go fetch your data.

For example, when you finish bingeing *The Mandalorian*, those specialist clerks are the ones who notice you watched it, pull your progress, and (if enabled) quietly file a rating on your behalf — and `TraktManager`'s clever move is to ask "how far has this person gotten in all their shows?" exactly once and then reuse that answer, instead of pestering Trakt 150 separate times about 150 separate shows. It's the dispatcher, not the worker.

## Interactions

- **Parent manager:** `Main` (`scripts/main.py`), which constructs it alongside the other top-level service managers and supplies the shared deps plus optional cross-service handles.
- **Child managers it owns:** `TraktInstanceManager` (`trakt/instances/`, credential/token validation) and `TraktAPIManager` (`trakt/api/`, the HTTP layer that in turn loads the history / ratings / recommendations / watchlist / progress / lookup / universe / analytics / lists / movies / shows / sync sub-managers).
- **Sibling services (injected handles, not owned):** `sonarr_apis`, `tautulli_api`, `plex_api`.
- **Brain:** reaches the ML layer only through the injected `ml_manager` handle and through auto-rating logic in the leaf managers; this class names no specific `machine_learning/` module directly.
