# SonarrSeriesSyncHistoryManager

- **File** — `scripts/managers/services/sonarr/series/sync/history.py`
- **One-liner** — Selects "recent" Sonarr series by querying the Sonarr history API with a smart, timestamp-driven since-date.

## What it does (for a senior Python engineer)

`SonarrSeriesSyncHistoryManager(BaseManager, ComponentManagerMixin)` is a submanager under `SonarrSeries`, loaded by `SonarrSeriesSyncManager` as `history`. Its job is FETCH: it asks Sonarr "what series had history events since date X" and returns the set of series ids.

**Init / deps.** Sets `parent_name = "SonarrSeries"`, calls `super().__init__`, `register()`, then resolves parent and pulls `sonarr_api`, `dry_run`, `orchestration`, `instance_manager`, plus the dual cache (`sonarr_cache` from `kwargs["cache_manager"]`/parent, `global_cache` from arg/parent). Raises `ValueError` if no logger could be resolved.

**Public methods.**
- `get_recent_sonarr_series(instance: str) -> set[int]` (decorated `@log_function_entry`, `@timeit("get_recent_sonarr_series")`). Resolves the instance, computes a `since_date`, calls the Sonarr history endpoint, and returns `{record["seriesId"] for record in records}`. This is the method `composite_sync_workflow` calls.
- `sync_series_from_history(instance: str, timestamp: str) -> None` (decorated). A standalone path that fetches `history/since` for a caller-supplied timestamp and *counts* import/rename events but does not apply anything — it only logs `updates_made`. Appears legacy/diagnostic.

**Since-date logic (the core of `get_recent_sonarr_series`).** Reads the last sync age via `global_cache.timestamp_handler.get_age_seconds("sonarr", resolved_instance, "history")`:
- If there is no timestamp handler, or `get_age_seconds` returns `None` (never recorded), it raises internally → falls back to a 7-day-ago since-date.
- If `age > 7*86400` (cache stale > 7 days), it clamps the since-date to 7 days ago and logs a warning.
- Otherwise the since-date is `now - age` (resume exactly where it left off), logged with an `Xh Ym` age.

**FETCH endpoint.** `get_recent_sonarr_series` calls `sonarr_api._make_request(resolved_instance, "history/since?date=<since>&includeSeries=true&includeEpisode=true", method="GET", fallback={})`. `sync_series_from_history` instead builds a raw `requests.get(f"{base_url}/api/v3/history/since", params=..., headers={"X-Api-Key": api})` from `config["sonarr_instances"][instance]`.

**CACHE writes.** After fetching, it calls `global_cache.timestamp_handler.update_timestamp("sonarr", resolved_instance, "history")` so the next run uses *now* as its since-date instead of re-falling-back to the 7-day window. No Parquet writes.

**Config keys.** `sync_series_from_history` reads `config["sonarr_instances"][instance]` for `base_url` and `api`.

**dry_run.** Captured but unused here — both methods are read-only (FETCH) plus a timestamp write; nothing is applied to Sonarr.

## How it functions

Lifecycle is the standard submanager pattern: construct → `register()` → inherit shared deps from the `SonarrSeries` parent → ready. The control flow of `get_recent_sonarr_series` is: resolve instance → compute since-date from the recorded timestamp (with stale-clamp and first-run fallback) → GET `history/since` → extract `records` → write the new timestamp → return the set of `seriesId` values. There is no machine_learning delegation in this file. The timestamp handler lives on `global_cache` and is the single source of "when did we last sync history for this instance".

## Criteria & examples

- **First run (no timestamp):** `get_age_seconds(...)` returns `None` → since-date = `now − 7 days`. Example: today is 2026-06-10, so `since_date = 2026-06-03T...`; whatever Sonarr imported in the last week seeds the sync.
- **Normal resume:** last synced 5 hours ago → `age = 18000s` (< 604800) → since-date = `now − 18000s`, logged as "age 5h 0m". Only series with events in those 5 hours are returned.
- **Stale clamp:** last synced 12 days ago → `age = 1036800s` (> 604800) → since-date clamped to `now − 7 days` and a warning is logged, so a long gap can't request an unbounded history window.
- **Event-type filter (in `sync_series_from_history` only):** counts events whose `eventType` is in `{downloadFolderImported, seriesFolderImported, episodeFileRenamed}`; all others are ignored.

## In plain English

This is the part that asks Sonarr, "what shows did you actually do anything with lately?" — for example, "which shows got new episodes downloaded since the last time we checked." It remembers the last time it asked (a bookmark), so next time it only asks about what changed since then. If it's never asked before, or it's been ages, it just asks about the last week so it doesn't drown in old events. It then hands back the list of those shows for the rest of the sync to act on.

## Interactions

- **Parent manager:** `SonarrSeries`; immediate caller `SonarrSeriesSyncManager.composite_sync_workflow`.
- **Sibling submanagers:** feeds series ids that `synchronize` later applies; an alternative selection source to `tautulli`.
- **Services:** `sonarr_api` (history endpoint), `instance_manager` (instance resolution), `global_cache.timestamp_handler` (sync bookmark).
- **Brain modules:** none.
