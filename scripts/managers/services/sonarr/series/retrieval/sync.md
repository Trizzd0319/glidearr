# SonarrSeriesRetrievalSyncManager

- **File** — `scripts/managers/services/sonarr/series/retrieval/sync.py`
- **One-liner** — Incremental sync: it fetches Sonarr history "since" a timestamp, figures out which series actually changed, re-fetches just those, and rewrites their letter-cache files.

## What it does (for a senior Python engineer)

`SonarrSeriesRetrievalSyncManager(BaseManager, ComponentManagerMixin)` is the delta-by-history path. Rather than re-pulling the whole library, it asks Sonarr's `history/since` endpoint what happened recently, derives the set of affected `seriesId`s from relevant event types, refetches only those series, and persists them.

**Position in the manager tree**
- Loaded by `SonarrSeriesRetrievalManager` as the `sync` component.
- `parent_name` derived → `"SonarrSeriesRetrievalSync"`.
- Deps off `kwargs["manager"]`: `sonarr_cache`, `global_cache`, `sonarr_api`, `dry_run`. Logs a warning if `manager` or `sonarr_api` did not resolve (sync then unavailable).

**FETCH / CACHE / APPLY** — FETCH (raw `requests.get` to Sonarr history; per-series refetch via the fetch manager) + CACHE (writes changed series to letter files; updates a global-cache timestamp). No APPLY back to Sonarr.

**External API endpoints**
- `GET {base_url}/api/v3/history/since` with params `date={timestamp}`, `includeSeries=true`, `includeEpisode=true`, header `X-Api-Key: {instance api key}`. Note: this method uses the `requests` library **directly** (not `sonarr_api._make_request`), reading `base_url`/`api` from config.
- Per changed series: `manager.series_fetch._fetch_series_by_id(resolved_instance, series_id)` → which GETs `series/{id}`.

**Public methods**
- `sync_series_from_history(instance, timestamp) -> int` — the only public entry. Resolves the instance, loads `config["sonarr_instances"][resolved_instance]` (errors and returns `0` if missing), GETs history since `timestamp`, collects `seriesId`s for the valid event types, refetches and re-saves each, updates the library timestamp, and returns the count of series actually updated.

**Valid event types** that flag a series for refresh: `downloadFolderImported`, `seriesFolderImported`, `episodeFileRenamed`.

**Config keys** — `sonarr_instances.<instance>.base_url` and `sonarr_instances.<instance>.api` (the API key).
**Cache keys** — writes via `sonarr_cache.series.save_series_to_letter_file(...)`; bumps `global_cache.update_timestamp(CacheKeyPaths.sonarr.SONARR_LIBRARY, instance=resolved_instance)`.
**dry_run** — captured into `self.dry_run` but **not** branched on in `sync_series_from_history`; the cache write/timestamp update happen regardless. (Worth noting: this is a CACHE-layer write, not a Sonarr APPLY, so it does not mutate the external service.)
**Concurrency** — synchronous loop over the changed-series set.

## How it functions

Lifecycle: `BaseManager` init, dep resolution, registration. Then `sync_series_from_history(instance, timestamp)` is called by an upstream scheduler/caller with a watermark timestamp.

Control flow:
1. Resolve instance; load its config block (bail with `0` if absent).
2. Build the `history/since` URL + params, `requests.get`, `raise_for_status`, parse JSON. Any exception → log error, return `0`.
3. Build `updated_series_ids = {seriesId for items whose eventType ∈ valid set and has a seriesId}`.
4. For each id, `series_fetch._fetch_series_by_id(...)`; if data returned, append and `save_series_to_letter_file`.
5. `global_cache.update_timestamp(SONARR_LIBRARY, instance=...)`.
6. Return `len(updated_data)`.

No `machine_learning` brain module is involved.

## Criteria & examples

- **Event-type filter:** only `downloadFolderImported`, `seriesFolderImported`, `episodeFileRenamed` count. Example: a history page with 50 items where 6 are `downloadFolderImported`, 2 are `grabbed`, and 1 is `seriesFolderImported` → 7 events qualify, but if 3 of those share the same `seriesId`, the deduped set is, say, 5 series → only 5 refetches happen.
- **Missing config guard:** `sync_series_from_history("ghost-instance", ts)` where `sonarr_instances` has no `ghost-instance` → logs `❌ No configuration found…` and returns `0` (no HTTP call).
- **Empty/failed history:** a network error or a `raise_for_status` failure → logged, returns `0`.
- **Counting:** the return value is the number of series for which a refetch returned data — a flagged id whose refetch returns falsy is not counted.

## In plain English

Instead of re-counting the entire DVD shelf every night, this is the clerk who reads the day's delivery and rename log and asks "what actually changed since yesterday afternoon?" If only three shows got a new episode imported or a folder renamed, the clerk pulls fresh record cards for just those three shows and re-files them — leaving the other thousand cards untouched. Then they stamp the log with "last checked at this time" so tomorrow's check knows where to start.

## Interactions

- **Parent manager:** `SonarrSeriesRetrievalManager`.
- **Siblings:** calls the `fetch` manager's `_fetch_series_by_id` (accessed as `manager.series_fetch`); writes through `sonarr_cache.series`.
- **Services:** raw `requests` to the Sonarr `history/since` REST endpoint (config-driven `base_url`/`api`); `instance_manager` (via `manager.instance_manager`); `global_cache` timestamp; `CacheKeyPaths.sonarr.SONARR_LIBRARY`.
- **Brain modules:** none.
