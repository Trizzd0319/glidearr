# SonarrMonitoringSeriesManager

- **File** — `scripts/managers/services/sonarr/monitoring/series.py`
- **One-liner** — Series-level monitoring control: reads which series are monitored, flips a single series or a batch monitored/unmonitored via the Sonarr API, and snapshots the monitored/unmonitored split into the cache.

## What it does (for a senior Python engineer)

`SonarrMonitoringSeriesManager(BaseManager, ComponentManagerMixin)`. Unlike most siblings it derives `parent_name` dynamically from its own class name: `"SonarrMonitoringSeriesManager"` → strips the trailing `"Manager"` → `parent_name = "SonarrMonitoringSeries"`. It then resolves that name in the registry and pulls `sonarr_api`, `logger`, `manager`, `global_cache`, `sonarr_cache` (the `cache_manager` kwarg, "dual-cache support"), `key_builder`, and `dry_run` from it. It raises `ValueError` if no logger could be resolved.

It performs **FETCH** (live series reads), **APPLY** (PUT to flip `monitored`), and **CACHE** (snapshot write). No submanagers loaded.

Public methods:
- `get_series_with_monitoring_status(instance) -> (monitored_list, unmonitored_list)` — calls `sonarr_api.get_all_series(instance)` and partitions on the `monitored` flag.
- `monitor_or_unmonitor_series(series_id, instance, monitored=True) -> bool` — GETs `series/<id>`, sets `data["monitored"] = monitored`, PUTs it back via `sonarr_api._make_request(instance, f"series/{id}", method="PUT", payload=data)`. Returns success bool; logs and returns `False` if the fetch or update fails.
- `bulk_update_monitoring_status(instance, ids_to_monitor, ids_to_unmonitor) -> dict` — loops both id lists through `monitor_or_unmonitor_series`, returning `{"monitored": [...], "unmonitored": [...], "failed": [...]}`.
- `run_monitoring_data_pull(instance)` — uses the arrapi client (`sonarr_api.get_api(instance).all_series()`) to split monitored/unmonitored series ids, then writes a snapshot to the cache key built from `CacheKeyPaths.sonarr.MONITORED_SYNC` (`sonarr/<instance>/sync/monitored`) via `set_with_pretty_output`. The payload is `{monitoredSeries:[ids], unmonitoredSeries:[ids], meta:{timestamp(UTC ISO), instance, monitoredCount, unmonitoredCount}}`. Raises `ValueError` if `sonarr_cache` is missing.

**API touched:** `sonarr_api.get_all_series(instance)`, `sonarr_api._make_request(instance, "series/<id>" [PUT])`, `sonarr_api.get_api(instance).all_series()`.
**Cache keys written:** `sonarr/<instance>/sync/monitored` (`MONITORED_SYNC`).
**Config keys read:** none directly.
**dry_run:** captured into `self.dry_run` but **not consulted** in any method here — `monitor_or_unmonitor_series` will issue the PUT regardless of dry_run. (Worth flagging: this is a real mutating-write path that does not honor dry_run, unlike the priority-queue and rules managers.)

## How it functions

Lifecycle: `__init__` (dynamic `parent_name`, dual-cache wiring, logger guard) → `register()`. There's no single orchestrating `run()`; callers pick a verb. The typical flow is `run_monitoring_data_pull(instance)` to refresh the cache snapshot, and `monitor_or_unmonitor_series` / `bulk_update_monitoring_status` to apply changes. No `machine_learning` brain module is invoked; the "which series to flip" decision is made by the caller (e.g. the priority-queue or rules managers), not here — this is the pure APPLY/FETCH adapter.

## Criteria & examples

There are no scoring thresholds in this file — it is mechanism, not policy. The only branching is success/failure handling:
- `monitor_or_unmonitor_series(123, "default", monitored=False)` where `series/123` exists → PUT succeeds → logs "🚫 Series 123 ... is now unmonitored." and returns `True`.
- Same call where `series/123` returns nothing → logs "⚠️ Unable to fetch series ID 123 ..." and returns `False` (no PUT attempted).
- `bulk_update_monitoring_status("default", [10, 11], [20])` where series 11 fails → returns `{"monitored": [10], "unmonitored": [20], "failed": [11]}`.

## In plain English

This is the hand on the switch. Other parts of the system decide *which* shows should be recorded or stopped; this manager actually walks up to Sonarr and flips the "monitor this show" toggle on or off, one show or a whole list at a time, and reports back which flips worked and which didn't. It can also take a quick census — "here are all the shows currently being recorded, here are the ones that aren't" — and write that headcount down for everyone else to read. Like a stagehand who throws the levers an electrician (the decision-makers) tells them to.

## Interactions

- **Parent:** `SonarrMonitoring` (resolved dynamically as `SonarrMonitoringSeries`).
- **Siblings:** `SonarrMonitoringBackfillManager` writes the **same** `MONITORED_SYNC` cache snapshot (bulk, across all instances); `SonarrMonitoringPriorityQueueManager`/`SonarrMonitoringRulesManager` are the decision-makers that drive series monitoring changes.
- **Services:** Sonarr API (both the raw `_make_request` path and the arrapi client), Sonarr cache (snapshot writes), `global_cache` (dual-cache injection).
- **Brain modules:** none.
