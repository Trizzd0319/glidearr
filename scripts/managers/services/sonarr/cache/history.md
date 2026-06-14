# SonarrCacheHistoryManager

- **File** — `scripts/managers/services/sonarr/cache/history.py`
- **One-liner** — Caches Sonarr download/import history and performs an incremental, timestamp-based history sync that merges newly-imported series into the library cache (with a full-reload fallback).

## What it does (for a senior Python engineer)

`SonarrCacheHistoryManager(BaseManager, ComponentManagerMixin)` is reachable as `sonarr_cache.history`.

Public methods:
- `refresh_history(instance, days_back=7)` — FETCH `self.sonarr_api.get_history(instance, days_back=...)`, CACHE to `sonarr/{instance}/history` as `{"meta": {}, "data": history}`.
- `get_recent_history(instance)` — read that key's `data` list back.
- `run_incremental_sync()` — iterate every key in `config["sonarr_instances"]` and call `sync_from_history(instance)`.
- `sync_from_history(instance)` — the core incremental path (see below).
- `get_episode_watch_counts(instance)` — tally `{episodeId: count}` from cached history items.

### `sync_from_history(instance)` flow
1. Read the instance config; bail with an error log if missing.
2. Require `self.key_builder` (cache-key builder pulled from the parent manager); bail if absent.
3. Build the library cache key via `key_builder.format_cache_key("sonarr", instance, "library")` and load it. Read `cached_data["movies"]` (the merged-series map — the key is literally named `movies` even for Sonarr) and `cached_data["meta"]["timestamp"]`.
4. **Fallback:** if there is no cached timestamp, log a warning and trigger a full reload via `self.manager.orchestration.run_series_data_pull()` + `run_episode_data_pull()`, then return.
5. Otherwise FETCH `GET {base_url}/api/v3/history/since?date={cached_timestamp}&includeSeries=true&includeEpisode=true` directly with `requests`, using the instance's `X-Api-Key` header.
6. Keep only items whose `eventType` is in `{"downloadFolderImported", "seriesFolderImported", "episodeFileRenamed"}` and that carry a `movies` payload.
7. Merge via `self.global_cache.deduplicate_entries(cached_series, new_series_items, id_field="id", instance=instance)` → `(merged, stats)`.
8. CACHE the merged map back with a fresh UTC `meta.timestamp`, and log `total/new/updated/skipped`.

FETCH / CACHE / APPLY: **FETCH + CACHE** (no Sonarr writes). External API: `self.sonarr_api.get_history(...)` and a direct `requests.get` to `/api/v3/history/since`. Config keys read: `sonarr_instances` (and per-instance `base_url` / `api`). Cache keys: `sonarr/{instance}/history` (refresh/get) and the `...library` key built by `key_builder` (incremental merge).

`dry_run`: captured in `__init__`; not relevant here (history caching is non-destructive). Note `sync_from_history`'s fallback path triggers the orchestration data-pulls, which do their own dry-run gating.

Notable footgun: the raw `requests.get` reads `instance_config["api"]` for the API key. Per the project's "Service-specific API naming" note the credential lives under a service-specific key; this code reads the literal `"api"` field of the instance config. The request URL is logged with the query string stripped, so the date param (not secret) is not leaked, and the key is sent only in the header.

## How it functions

Init sets `parent_name = "SonarrCache"`, wires the dual cache + `sonarr_api`/`logger`/`manager`/`dry_run`, and additionally captures `self.key_builder = getattr(self.manager, "key_builder", None)` (required by `sync_from_history`). No `load_components` (no submanagers). No decision is delegated to a `machine_learning` module — the only selection logic is the `valid_event_types` filter and the timestamp-vs-full-reload branch.

## Criteria & examples

- Event filter: a `downloadFolderImported` history item with a `movies` (series) payload is merged; a `grabbed` or `episodeFileDeleted` item is ignored.
- Fallback: a brand-new instance whose library cache has no `meta.timestamp` skips the `/history/since` call entirely and instead kicks off a full series + episode data pull.
- `get_episode_watch_counts`: history `[{episodeId:9}, {episodeId:9}, {episodeId:11}]` → `{9: 2, 11: 1}`.

## In plain English

Instead of re-reading your entire TV library every time, this clerk asks Sonarr only "what's new since I last checked?" — using a bookmark (timestamp) from the previous run. It then files just the newly-imported shows into the catalog and moves the bookmark forward. If there is no bookmark yet (a fresh setup), it gives up on the shortcut and tells the orchestration team to do a full top-to-bottom catalog rebuild instead.

## Interactions

- **Parent manager:** `SonarrCacheManager` (attached as `history`).
- **Services:** the `sonarr_api` gateway for `get_history`; a direct HTTP call to the Sonarr `/history/since` endpoint; `global_cache` (`deduplicate_entries`, `format_cache_key` via `key_builder`).
- **Sibling/orchestration:** the full-reload fallback calls `self.manager.orchestration.run_series_data_pull()` / `run_episode_data_pull()` (the orchestration layer, a separate subdirectory).
- **Brain modules:** none.
