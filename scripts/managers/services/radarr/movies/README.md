# RadarrMoviesManager

- **File** — `scripts/managers/services/radarr/movies/__init__.py`
- **One-liner** — The orchestrator for everything "movie-shaped" under Radarr: it loads and owns the nine movie submanagers (retrieval, sync, monitoring, quality, keywords, credits, enrich, dataframe, helper) and exposes a few thin convenience pass-throughs.

## What it does (for a senior Python engineer)

`RadarrMoviesManager(BaseManager, ComponentManagerMixin)` is the parent of the movie submanager tree. It is constructed by its parent Radarr service manager (the object that supplies `radarr_api` and `instance_manager`), and it in turn constructs the nine leaf managers in this directory.

Construction (in `__init__`):
- Calls `super().__init__(...)` (BaseManager singleton wiring) and `self.register()`.
- Resolves three shared dependencies from `kwargs` (falling back to the parent manager): `self.radarr_api`, `self.instance_manager`, and `self.dry_run`.
- Builds an `init_kwargs` dict carrying the shared deps (logger, config, global_cache, validator, registry, radarr_api, instance_manager, `manager=self`, dry_run) that is splatted into every child constructor.
- Declares `all_component_classes` — a dict mapping nine names to classes:
  - `credits` → `RadarrMovieCreditsExtractorManager`
  - `dataframe` → `RadarrMovieDataframeBuilderManager`
  - `enrich` → `RadarrMovieEnrichmentManager`
  - `helper` → `RadarrMoviesHelperManager`
  - `keywords` → `RadarrKeywordProcessorManager`
  - `monitoring` → `RadarrMoviesMonitoringManager`
  - `quality` → `RadarrMoviesQualityManager`
  - `retrieval` → `RadarrMoviesRetrievalManager`
  - `sync` → `RadarrMoviesSyncManager`
- Marks **all nine** as `critical_keys` (so every one of them is treated as critical), then calls `split_components(...)` to partition them into `critical_components` / `noncritical_components` (here, practically all critical).
- Instantiates each component, `setattr(self, name, instance)`, and sets a registry flag `radarr.movies.{name}_initialized` (True/False). A per-component `load_summary[name]` records "Loaded" or "Failed: {e}".
- Sets `self.all_components_loaded` = (all critical loaded) and the registry flag `radarr.movies_manager_initialized`.
- Emits one filtered component-summary log line via `log_filtered_component_summary(...)`.

Note: unlike a textbook `load_components(...)` call, this class hand-rolls the instantiation loop (the same effect — attach each child as an attribute, inject shared deps, set registry flags) rather than delegating to the mixin's `load_components` method.

Public methods (all are thin pass-throughs to the `retrieval` submanager):
- `get_all_movies(instance)` → `self.retrieval.get_all_movies(instance)`. FETCH of the full movie library.
- `get_movie_by_id(movie_id, instance)` → `self.retrieval.get_movie_by_id(movie_id, instance)`.
- `get_movie_tags_map(instance)` → fetches all movies via retrieval and returns `{movie["id"]: movie.get("tags", [])}`.

FETCH / CACHE / APPLY: this class itself only delegates; it performs no direct HTTP. Its children do FETCH (retrieval, keywords, credits, enrich, dataframe), CACHE (keywords/credits/enrich/dataframe/retrieval write global_cache), and APPLY (sync, monitoring, quality issue the PUT/POST/DELETE).

Config keys read: none directly. dry_run is read from kwargs/parent and propagated into every child via `init_kwargs`.

global_cache / Parquet: none written here directly (children own their cache keys). Registry flags written: `radarr.movies.{name}_initialized` ×9 and `radarr.movies_manager_initialized`.

Singleton/threading: BaseManager singleton semantics apply (cached by class + singleton_key). No threading of its own.

## How it functions

Lifecycle: parent Radarr manager constructs this class → `__init__` wires shared deps → builds `init_kwargs` → `split_components` partitions the nine classes → a critical loop then a non-critical loop instantiate each child, attach it as an attribute, and set its registry flag → `all_components_loaded` / `radarr.movies_manager_initialized` recorded → one summary line logged. After construction, callers reach any movie capability via `manager.retrieval`, `manager.sync`, `manager.quality`, etc., or via the three convenience pass-throughs.

It delegates no decision to a `machine_learning` brain module itself; value-judgements (scoring, demotion, monitor policy) are made by brain modules that *consume* the data these submanagers fetch/cache (e.g. the enriched movie list and the movie DataFrame).

## Criteria & examples

The only branching logic is the critical/non-critical load split. Because `critical_keys` lists all nine names, any child that throws during construction sets `all_critical_loaded = False`, which propagates to `radarr.movies_manager_initialized = False`. Example: if `RadarrMoviesQualityManager.__init__` raised, `load_summary["quality"]` becomes `"❌ Failed: <error>"`, the registry flag `radarr.movies.quality_initialized` is set False, and the manager reports itself as not fully loaded — but the other eight children still load.

## In plain English

Think of this as the manager of a movie department at a streaming service. The department head doesn't personally do every job — instead they hire nine specialists (one to look up movies, one to add/delete them, one to flip the "keep watching this" flag, one to grade quality, and so on) and make sure each shows up for work. If one specialist is out sick, the head notes it on the whiteboard but the rest of the team keeps running. When you ask the head "what movies do we have?", they just walk over and ask the lookup specialist.

## Interactions

- **Parent manager:** the Radarr service manager (supplies `radarr_api`, `instance_manager`, `dry_run`).
- **Sibling/child submanagers (all in this directory):** retrieval, sync, monitoring, quality, keywords, credits, enrich, dataframe, helper.
- **Other services / brain:** indirectly feeds `machine_learning` brain modules, which read the enriched movie list / DataFrame that the enrich and dataframe children cache.
