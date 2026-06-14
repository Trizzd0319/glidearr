# RadarrOrchestrationManager

- **File** — `scripts/managers/services/radarr/orchestration/__init__.py`
- **One-liner** — The Radarr "conductor": a single `run()` that drives the whole per-instance Radarr pipeline — pull every kind of data, cache it, enrich it with Trakt, build Parquet tables, then hand decisions to the space-pressure / universe quality managers.

## What it does (for a senior Python engineer)

`RadarrOrchestrationManager(BaseManager, ComponentManagerMixin)` is the top-level sequencer for everything Radarr does in a run. It declares `parent_name = "RadarrManager"` and is auto-linked under `RadarrManager` by `BaseManager`. It does **not** call `load_components` itself — it owns no submanagers. Instead it is a *fan-out coordinator*: it pulls its sibling Radarr submanagers (movies, quality, storage, monitoring, cache) and the Trakt/Tautulli managers **out of the `RegistryManager`** on demand and delegates the real work to them.

### Dependency wiring (`__init__`)
- `radarr_api` — resolved from (in order) the explicit `radarr_api` kwarg, `parent.radarr_api`, or `registry.get("manager", "RadarrManager")`. This is the `RadarrInstanceManager` used for direct HTTP fallbacks.
- `movies` — `parent.movies` or `registry.get("manager", "RadarrMoviesManager")`. The comment in code explicitly notes movies live on the **parent `RadarrManager`**, not on `radarr_api`.
- `dry_run` — taken from the `dry_run` kwarg, else inherited from the parent, else `False`.

### Registry-lookup helpers (lazy sibling resolution)
Each returns `None` on failure so callers degrade gracefully:
- `_all_instances()` → `list[str]` of configured instance names via `radarr_api.get_all_radarr_apis().keys()` (empty list if unavailable).
- `_resolve_instance(instance)` → canonical instance name via `radarr_api.resolve_instance(...)`, falling back to `instance or "default"`.
- `_get_cache_manager()` → `RadarrCacheManager`.
- `_get_movies_manager()` → `RadarrMoviesManager` (prefers the cached `self.movies`).
- `_get_quality_manager()` → `RadarrQualityManager`.
- `_get_storage_manager()` → `RadarrStorageManager`.
- `_get_monitoring_manager()` → top-level `RadarrMonitoringManager`.
- `_get_trakt_movies_manager()` → `TraktMoviesManager` (optional enrichment source; `None` when Trakt is not configured).

### Key public methods
Every method takes an `instance` and, with the exception of `run_enrichment`/`run_dataframe_build` (which loop all instances themselves), is normally called from `run()` with an already-resolved instance name. All data-pull methods follow the same pattern: **prefer the dedicated submanager; fall back to a direct `radarr_api._make_request(...)` call**; then **CACHE the result into `global_cache`**.

- `run_movie_data_pull(instance)` — pulls the full movie list (via `RadarrMoviesManager.retrieval.get_all_movies` or the `movie` endpoint). Writes `radarr.movies.<instance>.full`.
- `run_monitoring_data_pull(instance)` — gets monitored/unmonitored split (via `RadarrMonitoringManager.movies.get_monitoring_summary`, else by filtering the cached/fetched movie list on `monitored`). Writes `radarr.monitoring.<instance>` as `{"monitored", "unmonitored", "meta": {timestamp, monitoredCount, unmonitoredCount}}`.
- `run_quality_data_pull(instance)` — quality profiles (via `RadarrQualityManager.selector.get_quality_profiles`, else `qualityprofile` endpoint). Writes `radarr.quality.<instance>`.
- `run_tag_data_pull(instance)` — tags (always direct `tag` endpoint). Writes `radarr.tags.<instance>`.
- `run_custom_format_data_pull(instance)` — via `RadarrQualityManager.custom_formats.get_custom_formats`, else `customformat` endpoint. Writes `radarr.custom_formats.<instance>`.
- `run_storage_data_pull(instance)` — root-folder/disk data via `RadarrStorageManager.space.get_root_folders`, else `rootfolder` endpoint. Writes `radarr.disk.<instance>`.
- `run_adjustment_data_pull(instance)` — quality-definition adjustments via `RadarrQualityManager.adjustments.get_quality_adjustments`, else `qualitydefinition` endpoint. Writes `radarr.quality.adjustments.<instance>`.
- `run_keywords_data_pull(instance)` — keywords via `RadarrMoviesManager.keywords.get_keywords`; writes `radarr.keywords.<instance>` (no API fallback — skips with a debug log if the keywords submanager is missing).
- `run_credits_data_pull(instance)` — people/studios via `RadarrMoviesManager.credits.get_people_and_studios`; writes `radarr.credits.<instance>` (no fallback; skips if missing).
- `run_enrichment()` — loops **all** instances, builds enriched movies via `RadarrMoviesManager.enrich.build_enriched_movies`; writes `radarr.movies.<instance>.enriched`.
- `run_dataframe_build()` — loops **all** instances, builds a DataFrame via `RadarrMoviesManager.dataframe.build_movie_dataframe`; writes `radarr.movies.<instance>.dataframe`.
- `run_movie_files_pull(instance)` → `dict` stats — delegates to `RadarrCacheMovieFilesManager.run(resolved)` (via `cache_mgr.movie_files`, registry fallback). Builds/refreshes the `movie_files` Parquet. Returns `{}` and warns if the manager is unavailable.
- `run_relational_pull(instance)` → `dict` stats — builds the relational Parquet tables (people, relations, studios). See "How it functions" for the Trakt-enrichment branch.
- `run_movie_ratings(instance)` → `dict` — auto-rates watched movies on Trakt. See "How it functions".
- `run_refresh_scores(instance)` → `int` — delegates to `RadarrQualityManager.space_pressure.refresh_scores(resolved)`; recomputes watchability scores on every Parquet row and persists them. Returns count (0 if the manager is missing). **Must run before** `run_universe_quality` so the universe pass has valid scores to gate 4K eligibility.
- `run_space_pressure(instance)` → `dict` — delegates to `RadarrQualityManager.space_pressure.run(resolved)` (the downgrade-then-delete free-space pipeline). Returns `{}` if missing.
- `run_universe_quality(instance)` → `dict` — reads current free space (`RadarrStorageManager.space.get_free_space_per_instance()[resolved]`, default `0.0`) then delegates to `RadarrQualityManager.universe.run(resolved, free_gb)`. Returns `{}` if missing.
- `run(instance=None)` → `dict` — the orchestrated entry point (see below).
- `get_warmup_tasks()` → `dict[str, callable(instance)]` — returns named lambdas for cache warmup (`tags`, `quality_profiles`, `custom_formats`, `quality_definitions`, `disk`, `history`), each routing through the right submanager or a direct `_make_request` fallback. Note this is the only method exposing the `history` endpoint.

### FETCH / CACHE / APPLY
- **FETCH**: all `*_data_pull` methods and `get_warmup_tasks` (HTTP GETs against Radarr or delegated reads).
- **CACHE**: every `*_data_pull` writes a `radarr.*` `global_cache` key; the Parquet builds (`movie_files`, `relational`) and `refresh_scores` persist Parquet/scores.
- **APPLY**: this class never PUTs/DELETEs directly. APPLY decisions are pushed downstream to `space_pressure` (downgrade/delete), `universe` (upgrade/downgrade), and `TraktRatingsManager.auto_rate_watched_movies` (POSTing ratings to Trakt).

### External API endpoints touched (direct fallbacks via `radarr_api._make_request`)
`movie`, `qualityprofile`, `tag`, `customformat`, `rootfolder`, `qualitydefinition`, `history`. All other endpoints go through the dedicated submanagers.

### config keys read
- `daemons.enrich.enabled` — toggles cache-only Trakt behavior in `run_relational_pull`.
- `daemons.enrich.scope` — used by `_log_enrichment_eta` to count endpoints per movie.
- `rating_groups` — the set of group names whose `tmdb_completions` maps are merged in `run_movie_ratings` (defaults to `{"household": {}}` when unset).

### global_cache / Parquet keys
- **Writes**: `radarr.movies.<i>.full`, `radarr.monitoring.<i>`, `radarr.quality.<i>`, `radarr.tags.<i>`, `radarr.custom_formats.<i>`, `radarr.disk.<i>`, `radarr.quality.adjustments.<i>`, `radarr.keywords.<i>`, `radarr.credits.<i>`, `radarr.movies.<i>.enriched`, `radarr.movies.<i>.dataframe`. (`movie_files` and relational Parquets are written by the cache submanagers it delegates to.)
- **Reads** (in the Trakt/ratings paths): `radarr.movies.<i>.full`, `trakt/history/movies`, `tautulli/history/all`, `tautulli/affinity`, `tautulli/group/<group>/tmdb_completions`.

### dry_run, singleton, concurrency
- Captures `self.dry_run` (avoiding the BaseManager footgun) but does not itself branch on it — the actual "would …" logging happens in the delegated APPLY managers (space_pressure, universe, ratings).
- Singleton via `BaseManager` (`_instances` keyed by class + singleton_key); `self.register()` is called in `__init__`.
- No threading here; the sequence is strictly serial per instance. (Live Trakt fetching is offloaded to the background enrich daemon — see below.)

## How it functions

**Lifecycle**: `__init__` → `super().__init__` (injects shared deps, auto-links parent) → `self.register()` → resolve `radarr_api` / `movies` / `dry_run`. No `load_components`.

**Main control flow — `run(instance=None)`**: resolves the target instance list (single instance, or all instances when `None`); warns and returns `{}` if there are none. For each instance it runs a fixed ordered task list, each wrapped in a per-task `try/except` so one failure never aborts the rest (failures are recorded as `"error: <e>"` in the results dict):

1. `movie_data` → 2. `monitoring` → 3. `quality` → 4. `tags` → 5. `movie_files` → 6. `relational` → 7. `movie_ratings` → 8. `space_pressure` → 9. `refresh_scores` → 10. `universe`.

The ordering is load-bearing: movie/quality/tag pulls populate the caches that `movie_files` and `relational` consume; `relational`/`movie_ratings` enrich with Trakt; `refresh_scores` writes the scores that `universe` reads to gate 4K eligibility. (Note: `run_enrichment` and `run_dataframe_build` exist but are **not** part of the `run()` task list — they are standalone entry points.)

**`run_relational_pull` Trakt branch**: if `TraktMoviesManager` is registered, it loads the movie list from `radarr.movies.<i>.full` (or fetches it), then builds a *priority set* of already-watched movies — exact `tmdbId` matches from `trakt/history/movies` plus title-only matches from `tautulli/history/all` (Plex plays never scrobbled to Trakt). It then calls `trakt_movies.enrich_movies(movies, watched_titles, watched_tmdb_ids, chunk_size=500, cache_only=daemon_enabled)` and feeds the enriched list to `relational.build_relations_from_movies(...)`. When the enrich daemon is enabled, `cache_only=True` so the run reads only cached Trakt data and can never hang on a 429; `_log_enrichment_eta` then renders an ASCII callout table estimating full-coverage time. With no Trakt manager it falls back to a studios-only `relational.run(resolved)`.

**`run_movie_ratings` flow**: gathers movies (cache/API), merges per-group `tautulli/group/<group>/tmdb_completions` maps (keeping the highest `pct` per `tmdb_id`), reads `tautulli/affinity`, builds the watched-`tmdbId` set from `trakt/history/movies` plus the completion-map keys, then calls `TraktRatingsManager.auto_rate_watched_movies(movies, completion_map, watched_tmdb_ids, genre_affinity, people_manager=TraktMoviesManager.people)`. Bails early (returns `{}`) with an explanatory log if there are no movies, an empty completion map, or no `TraktRatingsManager`.

**Brain delegation (NOT documented here)**: the value-judgement work lives downstream in `machine_learning/`. `run_refresh_scores` / `run_space_pressure` / `run_universe_quality` delegate to `RadarrQualityManager.space_pressure` and `.universe`, which in turn route scoring and space-loop decisions through the `machine_learning/` brain (e.g. scoring, space planners). This orchestrator only sequences those calls and supplies the free-space input.

## Criteria & examples

- **Free-space gate (universe pass)**: `run_universe_quality` reads `free_gb` from `RadarrStorageManager.space.get_free_space_per_instance()` (default `0.0` if it can't be read) and passes it to `universe.run(resolved, free_gb)`. Example: with 12.0 GB free on instance `4k`, it logs `Universe quality pass for '4k' (12.0 GB free)` and lets the universe manager decide downgrades/upgrades against that figure.
- **Score-before-universe ordering**: `refresh_scores` runs at position 9, before `universe` at position 10, so the universe manager always sees fresh scores when gating 4K eligibility. If `refresh_scores` returned 0 (space-pressure manager missing), the universe pass still runs but against whatever scores already exist.
- **Completion-map merge rule**: when the same `tmdb_id` appears in two rating groups, the entry with the **higher (or equal) `pct`** wins. Example: `tmdb 27205` (Inception) at `pct=0.95` in group `household` and `pct=0.40` in group `kids` → the `0.95` record is kept.
- **Watched priority set (relational)**: a movie with `tmdbId=603` (The Matrix) present in `trakt/history/movies` is added to `watched_tmdb_ids`; a Plex-only play titled "the matrix" in `tautulli/history/all` lands in `watched_titles`. Both prioritize that movie's Trakt enrichment ahead of the `chunk_size=500` batch of everything else.
- **Daemon cache-only guard**: if `daemons.enrich.enabled` is true, `cache_only=True` is passed to `enrich_movies`, so no live Trakt calls are made during the run (preventing a 429 hang). The ETA table reports `CACHE-ONLY - zero live Trakt calls` and estimates full enrichment from owned-movie count × endpoints-per-movie against the daemon's safe throughput.
- **Graceful degradation**: every delegated submanager is fetched defensively. If `RadarrCacheMovieFilesManager` is absent, `run_movie_files_pull` logs a warning and returns `{}` rather than raising — and `run()`'s per-task `try/except` would catch it regardless.

## In plain English

Think of this class as the floor manager of a video-rental warehouse who, once a shift, walks the same checklist in the same order. First they take stock of every movie on the shelves and note which ones are flagged to keep an eye on. Then they enrich the catalog cards with extra details (who starred in *Inception*, which studio made it) by phoning a research service (Trakt) — but politely, only asking about films someone in the household actually watched first, and never calling so often they get put on hold. Then they look at how much shelf space is left: if it's getting tight, they hand the problem to specialist staff who decide whether to swap a film for a smaller-quality copy or, as a last resort, remove it. The floor manager doesn't make any of those keep/drop/upgrade judgement calls personally — they just make sure each specialist runs in the right order with the right information, and if one specialist is out sick, the rest of the checklist still gets done.

## Interactions

- **Parent manager**: `RadarrManager` (`parent_name = "RadarrManager"`); inherits its logger/config/cache/validator and reads `radarr_api`, `movies`, and `dry_run` from it.
- **Sibling Radarr submanagers (pulled from registry, not owned)**: `RadarrMoviesManager` (`.retrieval`, `.keywords`, `.credits`, `.enrich`, `.dataframe`), `RadarrQualityManager` (`.selector`, `.custom_formats`, `.adjustments`, `.space_pressure`, `.universe`), `RadarrStorageManager` (`.space`), `RadarrMonitoringManager` (`.movies`), `RadarrCacheManager` (`.movie_files`, `.relational`).
- **Other services**: `TraktMoviesManager` (`.people`) for cast/crew enrichment; `TraktRatingsManager` for auto-rating; `RadarrInstanceManager` (`radarr_api`) for direct HTTP fallbacks. Reads cached Tautulli/Trakt history and affinity data, and respects the background **enrich daemon** (`daemons.enrich`) which owns live Trakt fetching.
- **Brain modules (delegated, not documented here)**: scoring and space-loop decisions reached via `RadarrQualityManager.space_pressure` / `.universe`, which route into `machine_learning/`. This orchestrator only sequences the calls and supplies free-space input.
