# SonarrRepairAnomalyManager

**File** — `scripts/managers/services/sonarr/repair/anomaly.py`
**One-liner** — Detects and reports inconsistencies between Sonarr's live data and its cache (series present in one but not the other) and orphaned episode files, producing a read-only anomaly report.

## What it does (for a senior Python engineer)

`SonarrRepairAnomalyManager(BaseManager, ComponentManagerMixin)` is a leaf repair sub-manager under `SonarrRepairManager`. It is **detection/report only** — it FETCHes from both the live API and the cache, diffs them, and returns findings; it never writes.

- **Parent:** `self.parent_name = "SonarrRepair"`. Constructed by `SonarrRepairManager` (non-critical).
- **Deps:** wires `manager`, `sonarr_cache` (from `cache_manager` kwarg or parent), `global_cache`, `dry_run`, and `sonarr_api` (from kwarg or the registered parent). Note it passes `self.global_cache` into `super().__init__` rather than the raw `global_cache` param. Hard-requires a logger (raises `ValueError` otherwise).
- **Loads submanagers:** none.

Public methods:

- **`scan_for_metadata_anomalies()`** — for each instance in `sonarr_api.get_all_sonarr_apis()`, reads live `client.get_series()` and the per-service cache at the literal key `f"sonarr::{instance_name}::series"` (via `self.sonarr_cache.get`). It diffs the live title set against the cached title set in both directions and appends `("missing_in_cache", instance, [...])` and/or `("missing_in_live", instance, [...])` tuples. Returns the `anomalies` list.
- **`identify_orphaned_episodes()`** — for each instance, reads `client.get_episode_files()` and `client.get_episodes()`, computes the episode-file `episodeId`s not present among defined episode `id`s, and records `{"instance", "orphaned_ids", "timestamp"}` (UTC ISO) for any orphans. Returns a list of such dicts.
- **`generate_anomaly_report()`** — calls both scanners and returns `{"metadata": ..., "orphans": ...}`.

- API endpoints touched: `get_series`, `get_episode_files`, `get_episodes`.
- Config keys read: none.
- Cache keys: reads the per-service `sonarr_cache` at literal key `sonarr::<instance>::series` (note this is **not** a `CacheKeyPaths` constant — it is a hand-built string distinct from the `global_cache` `sonarr/<instance>/library` convention). No keys written.
- FETCH / CACHE / APPLY: **FETCH only** (reads live + cache; writes nothing).
- dry_run: stored but not consulted (the manager never mutates anyway).
- Singleton/threading: standard `BaseManager` singleton; no threading.

## How it functions

Lifecycle: `__init__` wires deps **before** calling `super().__init__` (so it can pass `self.global_cache` up), then `self.register()`, resolves `sonarr_api`/`logger` from the registered parent, enforces the logger precondition, logs an init line. The scanners loop over instances inside `try/except` so a single instance failure is logged and skipped, not fatal. The report method is a thin aggregator. No `machine_learning` brain module is involved.

## Criteria & examples

- **`missing_in_cache`:** a series title in the live API but absent from the cached title set. Example: live has `{"Andor","Severance"}`, cache has `{"Severance"}` → `("missing_in_cache", "sonarr_4k", ["Andor"])`.
- **`missing_in_live`:** the reverse — a cached title no longer in the live API. Example: cache `{"Andor","Old Show"}`, live `{"Andor"}` → `("missing_in_live", ..., ["Old Show"])`.
- **Orphaned episode file:** an episode-file `episodeId` with no matching defined episode `id`. Example: episode files reference `episodeId` set `{500,501}` while defined episode ids are `{500}` → orphan `[501]` recorded with a UTC timestamp.

## In plain English

This is the reconciliation auditor who compares two ledgers: the real, live list of shows on the server versus the quick-reference notes the app keeps. It points out shows that exist live but were never written into the notes, and notes that mention shows the server no longer has. It also flags episode files that point at episode entries that don't exist. It only produces a report — it never erases or edits anything — so it's the "here's what's out of sync" briefing, leaving the actual cleanup to other specialists.

## Interactions

- **Parent manager:** `SonarrRepairManager`.
- **Siblings:** the other `SonarrRepair*Manager` specialists (e.g. `SonarrRepairOrphansManager` and `SonarrRepairCacheManager` would act on what this reports).
- **Services:** the Sonarr per-instance API clients (`sonarr_api`) and the per-service `sonarr_cache`.
- **Brain modules:** none.
