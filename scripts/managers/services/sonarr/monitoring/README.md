# SonarrMonitoringManager

- **File** — `scripts/managers/services/sonarr/monitoring/__init__.py`
- **One-liner** — The Sonarr "monitoring" sub-tree orchestrator: it loads and owns the eight monitoring submanagers (episodes, rules, scheduler, series, audit, backfill, priority_queue, space_thresholds) that decide and apply which TV series/episodes Sonarr should keep watching for.

## What it does (for a senior Python engineer)

`SonarrMonitoringManager(BaseManager, ComponentManagerMixin)` is a `BaseManager` singleton whose declared `parent_name = "SonarrManager"` (so it auto-links under the top-level Sonarr service manager). Its sole job is composition: it constructs the eight monitoring submanagers and exposes each as an attribute (`self.episodes`, `self.rules`, `self.scheduler`, `self.series`, `self.audit`, `self.backfill`, `self.priority_queue`, `self.space_thresholds`).

It performs no FETCH / CACHE / APPLY of its own and touches no API endpoints, config keys, or cache keys directly — all of that lives in the children it loads. It is a pure wiring/orchestration node.

Key behavior in `__init__`:
- Captures `sonarr_api`, `sonarr_cache` (from the `cache_manager` kwarg, falling back to the parent manager's `sonarr_cache`), and `dry_run` (default `False`).
- Builds `init_kwargs` — the shared dependency bundle (`logger`, `config`, `global_cache`, `validator`, `registry`, `manager=self`, `sonarr_api`, `cache_manager=sonarr_cache`, `dry_run`) — and threads it identically into every child.
- Defines a `component_map` of the eight submanager classes and treats **all** of them as `critical_keys` (i.e. every component is critical).
- Calls `split_components(...)` (from `scripts/support/utilities/managers/component_splitter.py`) to partition the map into critical vs non-critical buckets; since all keys are critical, the non-critical bucket is normally empty.
- Instantiates each component in turn, `setattr`s it onto `self`, and sets a per-component registry flag `sonarr.monitoring.<name>_initialized` (True/False). It records a human-readable `self.load_summary[name]` of "✅ Loaded" or "❌ Failed: <exc>".
- A failure in any **critical** component flips `all_critical_loaded` to False; failures in non-critical components are logged but do not. The aggregate is published as registry flag `sonarr.monitoring_manager_initialized` and stored on `self.all_components_loaded`.
- Emits one filtered component summary line via `log_filtered_component_summary(service_name="Sonarr", ...)`.

Note: it does **not** use the standard `ComponentManagerMixin.load_components(...)` path — it implements its own load loop (so it can split critical/non-critical and set the `sonarr.monitoring.*` flags itself).

**Threading / singleton notes:** standard `BaseManager` singleton semantics — one instance per `(class, singleton_key)`. No threads spawned here; children that retry (scheduler) do so synchronously.

## How it functions

Lifecycle: `__init__` → `super().__init__` (injects shared deps, auto-links parent) → `self.register()` → capture api/cache/dry_run → build `init_kwargs` → `split_components` → load critical then non-critical components into attributes while setting registry flags → publish aggregate flag → log summary. There is no separate `run()` entry point on this class; callers reach the behavior by invoking methods on the child attributes (e.g. `self.priority_queue.run_across_all_instances()`, `self.scheduler.schedule_monitoring_jobs()`, `self.backfill.backfill_monitoring_status()`).

It delegates no decision to a `machine_learning` brain module directly; any brain delegation happens (if at all) inside the children, not here.

## Criteria & examples

The only "rule" here is the critical/non-critical gate: every one of the eight components is critical, so if (say) `space_thresholds` raises during construction, `all_critical_loaded` becomes `False`, the registry flag `sonarr.monitoring.space_thresholds_initialized` is set `False`, `sonarr.monitoring_manager_initialized` is set `False`, and `self.load_summary["space_thresholds"]` reads `"❌ Failed: <exception>"` — but the other seven still load and remain usable.

## In plain English

Think of this as the manager of a TV-watching department. It doesn't personally decide which shows to keep recording — instead it hires eight specialists (one who checks free disk space, one who ranks shows by importance, one who runs the daily check-up, etc.), hands each of them the same shared toolkit (the Sonarr connection, the cache, the logbook), and keeps a roll-call sheet noting whether each specialist showed up for work. If a critical specialist fails to clock in, it raises a flag so the rest of the system knows the department isn't fully staffed — but the specialists who did show up still do their jobs.

## Interactions

- **Parent:** `SonarrManager` (the top-level Sonarr service manager), via `parent_name` / registry auto-link.
- **Submanagers loaded (children):** `SonarrMonitoringEpisodesManager`, `SonarrMonitoringRulesManager`, `SonarrMonitoringSchedulerManager`, `SonarrMonitoringSeriesManager`, `SonarrMonitoringAuditManager`, `SonarrMonitoringBackfillManager`, `SonarrMonitoringPriorityQueueManager`, `SonarrMonitoringSpaceThresholdsManager`.
- **Shared services passed down:** the Sonarr API client (`sonarr_api`), the Sonarr cache manager (`sonarr_cache`), and the global registry/config/logger from `BaseManager`.
- **Brain modules:** none directly.
