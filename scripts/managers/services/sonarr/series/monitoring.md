# SonarrSeriesMonitoringManager

- **File** — `scripts/managers/services/sonarr/series/monitoring.py`
- **One-liner** — A currently-empty series-monitoring submanager: it wires up the standard shared dependencies but defines no operational methods yet.

## What it does (for a senior Python engineer)

`SonarrSeriesMonitoringManager(BaseManager, ComponentManagerMixin)` is a child of `SonarrSeriesManager`. As written it is a scaffold: `__init__` is its only method. It loads no submanagers and performs none of FETCH / CACHE / APPLY. The class name and `parent_name = "SonarrSeries"` reserve the "monitoring" slot in the series component map, but no monitoring logic (e.g. toggling episode/series `monitored` flags via the Sonarr API) is implemented here.

`__init__` behavior:
- Calls `super().__init__` and `register()`.
- Resolves `self.manager` from `kwargs["manager"]` or `registry.get("manager", self.parent_name)`.
- Falls back to the parent's `logger` if none was injected, and RAISES `ValueError` if there is still no logger ("could not initialize without logger").
- Resolves `dry_run`, `orchestration`, the dual cache (`sonarr_cache` + `global_cache`), `sonarr_api`, and `instance_manager` from kwargs or the parent.
- Logs a debug init line.

No public methods beyond construction. No `run()` / `prepare()` are defined, so the parent's component `run()`/`prepare()` fan-out simply skips this component (the loop checks `hasattr(comp, "run")`). Config keys read: none. global_cache / Parquet: none. dry_run: stored, unused. Singleton/threading: standard `BaseManager` singleton; nothing else.

## How it functions

Lifecycle is init-only: the parent `SonarrSeriesManager` constructs it via `load_components` (registry-flagged `sonarr.series.monitoring_initialized`), it wires its dependencies, registers, and then sits idle. Because it exposes neither `run` nor `prepare`, the parent's iteration over `self.components` never invokes it. There is no internal control flow and no `machine_learning` brain delegation to document.

## Criteria & examples

There are no thresholds, guards, or selection rules — the class contains no decision logic. The only enforced invariant is the constructor's logger check: if neither an injected logger nor a parent logger is available, construction fails with `ValueError("❌ SonarrSeriesMonitoringManager could not initialize without logger")`. For example, instantiating it standalone with `logger=None` and no `manager` in the registry would raise; constructed normally under `SonarrSeriesManager`, it inherits the shared logger and initializes cleanly.

## In plain English

This is an empty desk with the nameplate "Monitoring" already on it. The department (the series manager) has reserved a spot for a clerk whose future job will be deciding which shows and episodes Sonarr keeps an eye on, but right now no one is sitting there and the desk does nothing. It's set up and connected to the office systems, just waiting for its actual duties to be written.

## Interactions

- **Parent manager**: `SonarrSeriesManager` (constructs it, supplies all shared deps).
- **Sibling submanagers**: none used; it holds references (`manager`, `sonarr_api`, `instance_manager`, caches, `orchestration`) but invokes nothing.
- **Brain modules**: none.
- **Other services**: none exercised in current code.
