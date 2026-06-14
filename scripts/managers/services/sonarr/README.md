# SonarrManager

- **File** — `scripts/managers/services/sonarr/__init__.py`
- **One-liner** — The top-level Sonarr service manager: a thin orchestrator that wires up and runs the whole tree of Sonarr submanagers (instance/cache/series/episodes/monitoring/storage/repair/validator/orchestration) for one process run.

> **Note:** Sonarr is **single-instance** (one `sonarr` instance) — there is **no** cross-instance tier
> routing or series migration. Quality is governed **per-episode** by JIT (the per-episode quality
> profile / resolution markers), not by routing shows across resolution-tiered instances.

## What it does (for a senior Python engineer)

`SonarrManager(BaseManager, ComponentManagerMixin)` is one of the top-level service managers constructed by `Main` in `scripts/main.py` (after the parallel Radarr/Sonarr/Trakt auth check, alongside Radarr and Trakt). It owns no media-FETCH/CACHE/APPLY logic itself — it is a **composition root** for the Sonarr subtree. The real verbs live in the submanagers it loads.

**Where it sits in the manager tree**
- **Parent:** `Main` (`scripts/main.py`); `Main` calls `SonarrManager(logger, config, global_cache, validator, registry, dry_run)` then `registry.set_flag("sonarr_initialized")`.
- **Children (submanagers)** — declared as typed class attributes and instantiated from this file's sibling modules:
  - `instance_manager` → `SonarrInstanceManager` (the `sonarr_api` reference; created eagerly in `__init__`)
  - `sonarr_cache` → `SonarrCacheManager` (created eagerly in `__init__`; **not** a loadable component)
  - `validator_manager` → `SonarrValidatorManager`
  - `series` → `SonarrSeriesManager`
  - `episodes` → `SonarrEpisodesManager`
  - `monitoring` → `SonarrMonitoringManager`
  - `storage` → `SonarrStorageManager`
  - `repair` → `SonarrRepairManager`
  - `orchestration` → `SonarrOrchestrationManager`
  - `quality` → `SonarrQualityManager` is present in `full_components` but is **filtered out** of the active set (it is not a key in `component_dependencies`), so it is not loaded/prepared/run by this manager in the current configuration.
  - Each of the above lives in its own sibling module/subdirectory and is documented as a separate work item.

**Key public methods**
- `__init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs)` — sets `self.cache = global_cache` (BaseManager expects `cache`), calls `super().__init__`, reads `dry_run` from `kwargs` (default `False`), builds a `CacheKeyBuilder()` as `self.key_builder`, eagerly constructs `instance_manager` and `sonarr_cache`, assembles the shared `init_args` dict, and pre-splits the component classes into critical/non-critical sets via `split_components(...)`. Does not perform any HTTP or cache I/O beyond `sonarr_cache.initialize_cache_structure(include_optionals=True)`.
- `prepare(self)` — marks eagerly-built components as loaded (so they don't render ❌ in the summary), lazily loads any still-missing component via `_load_component`, then calls `.prepare()` on each component that has one. A `prepare()` exception flips that component to ❌ and is logged (no longer swallowed). Emits a colour-coded "N/M components prepared" summary. Decorated with `@timeit("prepare")`.
- `run(self)` — iterates `component_dependencies` in declared order, ensures each component is loaded (lazy `_load_component` fallback), calls `.run()` on each that has one, records ✅/❌ per component, and emits a filtered component summary via `log_filtered_component_summary(service_name="Sonarr", ...)`. Decorated with `@timeit("run")`. This is the entry point `Main` drives.

**Internal helper**
- `_load_component(self, name, auto_load_deps=True, log_dependencies=True)` — idempotent lazy loader. Returns an already-set attribute if present; otherwise checks the `RegistryManager` `"manager"` category for an existing singleton, else looks the class up in `critical_components`/`noncritical_components`, recursively loads declared dependencies first, then builds the instance via `self._singleton(name, component_class, **self.init_args)` (the BaseManager singleton cache). Records a `load_summary` row (✅ / ❌ / "❌ unknown").

**FETCH / CACHE / APPLY** — none directly. `SonarrManager` is pure orchestration; FETCH (Sonarr HTTP GETs), CACHE (Parquet / global_cache writes), and APPLY (PUT/DELETE/POST) are all delegated to the submanagers. It does trigger one cache-structure init: `self.sonarr_cache.initialize_cache_structure(include_optionals=True)`.

**External API endpoints touched** — none directly (no HTTP in this file; `instance_manager` is the `sonarr_api`).

**Config keys read** — none read by name in this file; `config` (ConfigManager) is passed straight through to every submanager. `dry_run` arrives as a constructor kwarg from `Main` (which reads it from config).

**global_cache / Parquet keys** — none read or written by name here; the only cache action is delegating `initialize_cache_structure(include_optionals=True)` to `SonarrCacheManager`.

**dry_run behavior** — `self.dry_run` is captured from kwargs and propagated explicitly into `instance_manager`, `sonarr_cache`, and the shared `init_args` (so every downstream component receives it). The inline comment notes this is load-bearing: without passing `dry_run` into `SonarrCacheManager`, its episode-file ops (acquisition, sync, JIT) ran LIVE even in dry_run sessions. `SonarrManager` itself mutates nothing.

**Singleton / concurrency / threading notes** — `BaseManager` is a process-wide singleton keyed by `(class, singleton_key)`; submanagers are built through `self._singleton(...)`, so repeated `_load_component` calls return the same instance, and an instance already registered in the `RegistryManager` `"manager"` category is reused. No threading inside this file (the parallel auth check happens earlier, in `Main`).

## How it functions

Lifecycle:

1. **`__init__`**
   - `self.cache = global_cache`, then `super().__init__(...)` (injects logger/config/cache/validator/registry, self-registers under the registry `"manager"` category, auto-links to parent).
   - `self.dry_run`, `self.load_summary = {}`, `self.key_builder = CacheKeyBuilder()`.
   - **Step 1** defines `component_dependencies` — the active subset and load order. Notable: `sonarr_cache` is deliberately **absent** here (it is hand-built, not a loadable component) and the comment explains `"cache"` is intentionally omitted so `prepare()` does not invoke `GlobalCacheManager.prepare()`.
   - **Step 2** eagerly builds `instance_manager` (`SonarrInstanceManager`), sets `instance_manager.sonarr_api = instance_manager` and `self.sonarr_api = instance_manager` (the service-specific `sonarr_api` reference), and wires the cache back-reference with `set_sonarr_cache(...)`.
   - **Step 3** eagerly builds `sonarr_cache` (`SonarrCacheManager`) and calls `initialize_cache_structure(include_optionals=True)`.
   - **Step 4** assembles `init_args` (the shared kwargs every component receives: logger, config, global_cache, validator, registry, dry_run, key_builder, sonarr_api, instance_manager, cache_manager=sonarr_cache, parent_name, manager=self), filters `full_components` down to `enabled_keys`, declares `critical_keys`, then calls `split_components(...)` to partition classes into `critical_components` / `noncritical_components`.
2. **`prepare()`** — loads any missing components, then prepares each.
3. **`run()`** — loads (if needed) and runs each component in dependency order, recording a summary.

The dependency graph encodes ordering constraints, e.g. `episodes` depends on `series` + `instance_manager`; `monitoring` depends on `instance_manager` + `series` + `episodes`; `orchestration` depends on `series` + `episodes` + `storage`. The inline comment notes monitoring/repair/validator must load before orchestration so `SonarrOrchestrationManager` can find them on `self` at init.

**Brain delegation** — `SonarrManager` itself delegates nothing to `machine_learning/`. Any value-judgement / decision delegation (e.g. monitoring policy, next-episode selection, pilot stepping) happens inside the individual submanagers and is documented in their own files; the brain modules themselves are out of scope here.

## Criteria & examples

This file contains orchestration rules rather than scoring thresholds:

- **Active-component filter** — only keys present in `component_dependencies` are activated (`enabled_keys`). Example: `quality` (`SonarrQualityManager`) is listed in `full_components` but absent from `component_dependencies`, so it is filtered out of `all_component_classes` and is never loaded, prepared, or run by this manager.
- **Eager-vs-lazy guard in `prepare()`** — components built eagerly in `__init__` (e.g. `instance_manager`) never pass through `_load_component`, which is the only place a `load_summary` row is written. Example: without the `prepare()` fix-up, `instance_manager` would render ❌ in the summary despite being healthy; `prepare()` therefore force-stamps any already-present component to `"✅"`.
- **Failure isolation in `run()`** — if `episodes.run()` raises, `results["episodes"] = "❌"` and the error is logged, but the loop still proceeds to `monitoring`, `storage`, etc. `all_ok` becomes `False` and the final summary reflects the partial failure.
- **Dependency auto-load** — calling `run()` on a fresh manager where, say, `monitoring` was never explicitly loaded triggers `_load_component("monitoring")`, which first ensures `instance_manager`, `series`, and `episodes` are loaded (recursing as needed) before constructing monitoring.

## In plain English

Think of `SonarrManager` as the **stage manager** for the "Sonarr" act of the show — it doesn't act in any scene itself, it just makes sure every cast member is present, in costume, and walks on stage in the right order. Before the curtain goes up it hires the doorman (`instance_manager`, who actually talks to your Sonarr server) and the props person (`sonarr_cache`, who keeps the notes about your shows). Then, when it's time to perform, it sends each specialist on in turn — the one who tracks your series, the one who handles episodes, the one who decides what to keep watching, the one who fixes broken downloads — and keeps a tidy checklist of who showed up (✅) and who tripped (❌). If one performer flubs their line, the show goes on; the others still do their parts. And during a "rehearsal" run (dry_run), everyone goes through the motions but nothing is actually changed on your server — so you can see what *would* happen to your TV library before committing to it.

## Interactions

- **Parent manager** — `Main` (`scripts/main.py`), which constructs it after Radarr and sets the `sonarr_initialized` registry flag.
- **Sibling top-level services** — `RadarrManager`, `TraktManager`, `TautulliManager`, and the opt-in Phase-3 managers (e.g. `AcquisitionManager`), all peers under `Main`.
- **Child submanagers (loaded here)** — `SonarrInstanceManager` (the `sonarr_api`), `SonarrCacheManager`, `SonarrSeriesManager`, `SonarrEpisodesManager`, `SonarrMonitoringManager`, `SonarrStorageManager`, `SonarrRepairManager`, `SonarrValidatorManager`, `SonarrOrchestrationManager`. (`SonarrQualityManager` exists but is currently filtered out.)
- **Shared infrastructure** — `ConfigManager`, `GlobalCacheManager` (as `cache`), `RegistryManager` (`"manager"` category, singleton reuse), `CacheKeyBuilder`, `LoggerManager`, the `@timeit` timing decorator, and the `split_components` utility.
- **Brain** — none directly; decision delegation into `machine_learning/` happens inside the individual submanagers, not in this orchestrator.
