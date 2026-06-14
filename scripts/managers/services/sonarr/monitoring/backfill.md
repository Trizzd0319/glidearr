# SonarrMonitoringBackfillManager

- **File** — `scripts/managers/services/sonarr/monitoring/backfill.py`
- **One-liner** — One-shot cache backfiller: walks every Sonarr instance and (re)populates the monitored/unmonitored-series snapshot in the cache so the rest of the monitoring tree has fresh data to read.

## What it does (for a senior Python engineer)

`SonarrMonitoringBackfillManager(BaseManager, ComponentManagerMixin)`, `parent_name = "SonarrMonitoring"`. Resolves parent from `manager` kwarg/registry; pulls `sonarr_api`, `sonarr_cache`, `dry_run`. Loads no submanagers.

It is a pure **FETCH → CACHE** manager: it reads live series from each instance and writes the monitored/unmonitored split to the cache. No APPLY (it never mutates Sonarr state).

Public method:
- `backfill_monitoring_status()` — the only entry point. If `sonarr_api` is missing it warns and returns. Otherwise it iterates `sonarr_api.get_all_sonarr_apis().items()` (each value is an arrapi client), calls `arrapi_client.all_series()`, partitions ids on `s.monitored`, and for each instance writes the cache key from `CacheKeyPaths.sonarr.MONITORED_SYNC` (`sonarr/<instance>/sync/monitored`) via `set_with_pretty_output`. The payload mirrors `SonarrMonitoringSeriesManager.run_monitoring_data_pull`: `{monitoredSeries:[ids], unmonitoredSeries:[ids], meta:{timestamp, instance, monitoredCount, unmonitoredCount}}`. Each instance is wrapped in try/except so one instance failing only logs a warning and continues. Raises `ValueError` if `sonarr_cache` is missing.

**API touched:** `sonarr_api.get_all_sonarr_apis()`, `arrapi_client.all_series()`.
**Cache keys written:** `sonarr/<instance>/sync/monitored` (`MONITORED_SYNC`), one per instance.
**Config keys read:** none.
**dry_run:** captured into `self.dry_run` but **not consulted** — backfill only writes the cache (it never PUTs to Sonarr), so there is nothing to suppress under dry_run.

Note: `meta.timestamp` uses `datetime.utcnow().isoformat()` (naive UTC), whereas the otherwise-identical `SonarrMonitoringSeriesManager.run_monitoring_data_pull` uses `datetime.now(timezone.utc).isoformat()` (tz-aware). Same cache key and shape, slightly different timestamp format.

## How it functions

Lifecycle: standard `__init__` → `register()` → resolve deps. The whole job is `backfill_monitoring_status()`: a single pass over all instances populating the monitored-sync cache. It is the "warm the cache from scratch" counterpart to the per-instance `SonarrMonitoringSeriesManager.run_monitoring_data_pull`. No `machine_learning` brain module is involved — there is no decision here, only a fetch-and-store.

## Criteria & examples

No thresholds or selection rules. The only control flow is per-instance error isolation:
- Instances `default` and `anime` both reachable → two cache keys written, e.g. `sonarr/default/sync/monitored` with `{monitoredSeries:[...42 ids...], unmonitoredSeries:[...8 ids...], meta:{monitoredCount:42, unmonitoredCount:8, ...}}`.
- If the `anime` instance's `all_series()` raises (e.g. API down), it logs "❌ Failed to backfill monitoring data for anime: <exc>" and still completes the `default` backfill.

## In plain English

Think of this as the "rebuild the index" button. When the system first starts up (or the cached list of which shows are being recorded has gone stale), this manager asks each Sonarr server "give me your full list of shows," sorts them into recording vs. not-recording piles, and writes those two piles down so everything else can read them instantly instead of asking Sonarr again. If one server is offline it just skips it and finishes the rest. It's bookkeeping, not decision-making — it never turns any show's recording on or off.

## Interactions

- **Parent:** `SonarrMonitoring` (`SonarrMonitoringManager`).
- **Siblings:** writes the **same** `MONITORED_SYNC` cache key as `SonarrMonitoringSeriesManager.run_monitoring_data_pull` (this is the bulk all-instances version); `SonarrMonitoringPriorityQueueManager` consumes monitored/unmonitored data downstream.
- **Services:** Sonarr API (arrapi clients per instance), Sonarr cache (snapshot writes).
- **Brain modules:** none.
