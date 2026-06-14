# SonarrSeriesManager

- **File** — `scripts/managers/services/sonarr/series/__init__.py`
- **One-liner** — The series-domain umbrella manager for Sonarr: it loads and orchestrates the six series submanagers (helpers, retrieval, monitoring, quality, space_pressure, sync) under one shared dependency set.

## What it does (for a senior Python engineer)

`SonarrSeriesManager(BaseManager, ComponentManagerMixin)` is the parent of the entire `series/` subtree. It is itself a child of the top-level Sonarr service manager (its `parent_name` class attribute is `"SonarrSeries"`, but `__init__` overwrites the instance attribute with the literal class name `"SonarrSeriesManager"`). It performs no FETCH / CACHE / APPLY of its own — it is a pure composition + fan-out node.

Key public methods:
- `__init__(...)` — resolves the dual-cache pair (`global_cache` plus `sonarr_cache`, the latter pulled from `kwargs["cache_manager"]` or the parent's `sonarr_cache`), resolves `sonarr_api` and `instance_manager` from kwargs or the parent, reads `dry_run` from kwargs (default `False`), then calls `load_components(...)` to instantiate all six submanagers.
- `prepare()` — iterates `self.components` and calls `prepare()` on each submanager that defines one, swallowing per-component failures as warnings so one bad component cannot abort the rest.
- `run()` — iterates `self.components` and calls `run()` on each submanager that defines one; per-component exceptions are logged as errors and do not abort the loop.

Submanagers loaded via `load_components` (component_map), with `registry_prefix="sonarr.series"` and `api_kwarg_name="sonarr_api"`:
- `helpers` → `SonarrSeriesHelpersManager`
- `retrieval` → `SonarrSeriesRetrievalManager`
- `monitoring` → `SonarrSeriesMonitoringManager`
- `quality` → `SonarrSeriesQualityManager`
- `space_pressure` → `SonarrSpacePressureManager`
- `sync` → `SonarrSeriesSyncManager`

Each submanager is attached as an attribute on `self` (e.g. `self.quality`, `self.retrieval`) and is given a `"sonarr.series.<name>_initialized"` registry flag by the mixin. The shared `init_args` dict it threads down includes `manager=self`, the resolved `sonarr_api`, `instance_manager`, both caches, `validator`, `registry`, and `dry_run`.

- **Config keys read**: none directly (it forwards the injected `config`).
- **global_cache / Parquet keys**: none read or written here.
- **dry_run**: stored and forwarded; this class never applies anything itself.
- **Singleton/threading**: standard `BaseManager` singleton semantics (cached in `_instances` by class + singleton_key); no extra threading. `run()`/`prepare()` are sequential.

## How it functions

Lifecycle: `__init__` → `super().__init__` (BaseManager wiring + auto-link to parent) → `register()` → resolve caches/api/instance_manager → build `init_args` → `load_components(...)` (which constructs, attaches, and registry-flags all six submanagers and logs one summary line). Later, the top-level Sonarr manager calls `prepare()` (best-effort fan-out) and `run()` (best-effort fan-out) on this object.

The `run()` fan-out is deliberately tolerant: a component without a `run()` method is skipped, and a component whose `run()` raises is logged and stepped over. Note that the heavy work in this subtree (the active-watcher quality upgrades and the space-pressure downgrades) is NOT triggered by this `run()` loop — those submanagers expose no-op `run()` methods and are instead driven later by the Sonarr orchestration layer after watchability scores are refreshed.

No decisions are delegated to a `machine_learning` brain module at this level; delegation happens inside the `quality` and `space_pressure` children.

## Criteria & examples

There are no thresholds or selection rules here — this is a wiring/orchestration shell. The only "rule" is the order of the component map (helpers, retrieval, monitoring, quality, space_pressure, sync), which is the order `prepare()`/`run()` iterate. For example, if `monitoring.run()` raises, the loop logs `❌ Failed to run 'monitoring'` and still proceeds to `quality`, `space_pressure`, and `sync`.

## In plain English

Think of this as the manager of a TV-show department who personally does none of the filing, fetching, or grading — they just hire and supervise six specialist clerks (one who looks up show IDs, one who pulls show records, one who tracks what you watch, one who grades picture quality, one who shrinks shows when the drive is full, and one who syncs watch history). When the boss says "get ready" or "go," this manager taps each clerk on the shoulder in turn. If one clerk trips, the others still do their jobs.

## Interactions

- **Parent manager**: the top-level Sonarr service manager (auto-linked via `BaseManager`).
- **Sibling/child submanagers**: `SonarrSeriesHelpersManager`, `SonarrSeriesRetrievalManager`, `SonarrSeriesMonitoringManager`, `SonarrSeriesQualityManager`, `SonarrSpacePressureManager`, `SonarrSeriesSyncManager`.
- **Brain modules**: none directly; its `quality` and `space_pressure` children delegate to `machine_learning.space.*`.
- **Other services**: indirectly, through the children, it touches Sonarr's HTTP API and the episode_files Parquet store.
