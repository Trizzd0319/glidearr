# RadarrCacheManager

- **File** — `scripts/managers/services/radarr/cache/__init__.py`
- **One-liner** — The cache sub-tree's umbrella manager: it instantiates and holds the eight Radarr cache submanagers (history, instance, monitoring, movie_files, orchestration, quality, relational, tags) and reports a one-line load summary.

## What it does (for a senior Python engineer)

`RadarrCacheManager(BaseManager, ComponentManagerMixin)` is a coordinator/aggregator, not a worker — it performs no FETCH / CACHE / APPLY itself. Its whole job is to build the cache submanager tree and expose each submanager as an attribute on `self` (e.g. `self.history`, `self.movie_files`, `self.quality`).

Where it sits in the tree:
- **Parent**: `RadarrManager` (`scripts/managers/services/radarr/__init__.py`), which constructs it as `self.radarr_cache`.
- **Children** (built in `__init__`, attribute names are the dict keys):
  - `history` → `RadarrHistoryCacheManager`
  - `instance` → `RadarrInstanceCacheManager`
  - `monitoring` → `RadarrMonitoringCacheManager`
  - `movie_files` → `RadarrCacheMovieFilesManager`
  - `orchestration` → `RadarrOrchestrationCacheManager`
  - `quality` → `RadarrQualityCacheManager`
  - `relational` → `RadarrCacheRelationalManager`
  - `tags` → `RadarrTagCacheManager`

Notable: it does NOT use `ComponentManagerMixin.load_components` despite mixing it in. Instead it hand-rolls the component build using `split_components(...)` from `scripts.support.utilities.managers.component_splitter`, which partitions the component dict into `critical` vs `noncritical` buckets (here every key is in `critical_keys`, so all eight are critical). It then loops each bucket, calls `cls(**init_kwargs)`, sets the instance as an attribute, and flips a registry flag.

Public/observable behavior:
- Builds `init_kwargs` once (logger, config, global_cache, validator, registry, plus `radarr_api`, `instance_manager`, `manager=self`, `dry_run`) and passes the SAME dict to every child, so the whole sub-tree shares one set of deps.
- Resolves `radarr_api` / `instance_manager` from kwargs first, then from the parent manager via `getattr(parent, ...)`.
- Sets `self.dry_run` BEFORE constructing children (line comment notes this is required so children can read it via `getattr(manager, "dry_run")`).
- Per child sets registry flag `radarr.cache.<name>_initialized` = True/False.
- Sets `self.all_components_loaded` from whether every critical child loaded, and registry flag `radarr.cache_manager_initialized` to the same.
- Records a `self.load_summary` dict like `{"history": "✅ Loaded", "tags": "❌ Failed: <err>"}`.
- Calls `self.log_filtered_component_summary(...)` (a mixin helper) to emit one Radarr-labelled summary line.

Config keys read: none directly (it reads `dry_run` only via kwargs/parent). Global_cache / Parquet keys: none directly. External API endpoints: none.

`dry_run`: captured and propagated to children; this class itself does nothing destructive.

Singleton/concurrency: as a `BaseManager` it is a process-wide singleton keyed by `(class, singleton_key)`; it self-registers under the registry "manager" category.

## How it functions

Lifecycle is entirely in `__init__`:
1. `super().__init__(...)` (BaseManager wiring: logger/config/cache/validator/registry, auto-link to parent), then `self.register()`.
2. Resolve `radarr_api`, `instance_manager`, `dry_run` from kwargs-or-parent; set `self.dry_run` early.
3. Define `all_component_classes` (the eight) and `critical_keys = set(all_component_classes)`.
4. `split_components(...)` partitions into critical/noncritical.
5. Two loops construct each component inside try/except, attach it, and set the per-component registry flag; failures are caught and recorded in `load_summary` without aborting the others (critical failures flip `all_critical_loaded` to False).
6. `log_filtered_component_summary(...)` prints the summary.

There is no `run()` method here — the actual work lives in the children (e.g. `self.movie_files.run(instance)`), driven by `RadarrManager`/orchestration. No machine_learning brain calls are made directly by this file.

## Criteria & examples

- All eight components are treated as **critical**. If, say, `RadarrCacheMovieFilesManager.__init__` raises (its init raises `ValueError` when it cannot resolve `dry_run`), `load_summary["movie_files"]` becomes `"❌ Failed: <err>"`, registry flag `radarr.cache.movie_files_initialized` is set False, `all_critical_loaded` becomes False, and `radarr.cache_manager_initialized` is set False — but the other seven still load.
- Worked example: with seven successes and one failure, `self.all_components_loaded == False`, yet `self.tags`, `self.history`, etc. are still usable.

## In plain English

Think of this as the manager of a film archive's record-keeping department. It doesn't file any paperwork itself — instead it hires eight specialist clerks (one for tags, one for quality profiles, one for the big movie-files ledger, etc.), hands each the same office keys and the same "are we just rehearsing today?" (dry-run) note, and then posts a sign-in sheet showing which clerks showed up for work. If the movie-files clerk calls in sick, the others still open the office; the sign-in sheet just shows one absence.

## Interactions

- **Parent**: `RadarrManager` (creates it as `radarr_cache`).
- **Children**: the eight cache submanagers listed above; it shares one logger/config/cache/validator/registry and the `radarr_api` + `instance_manager` with all of them.
- **Helpers**: `split_components` (component partitioning), `ComponentManagerMixin.log_filtered_component_summary` (summary line), `timeit`, `LoggerManager`.
- **Brain modules**: none directly (its `movie_files` and `relational` children are the ones that delegate into `machine_learning/`).
