# RadarrHistoryCacheManager

- **File** — `scripts/managers/services/radarr/cache/history.py`
- **One-liner** — Caches Radarr event history per instance and performs an incremental library-cache sync by pulling only `history/since` the last cached timestamp.

## What it does (for a senior Python engineer)

`RadarrHistoryCacheManager(BaseManager, ComponentManagerMixin)` is a history/incremental-sync adapter. It performs FETCH (GET `history`, and a raw `requests.get` against `history/since`), CACHE (writes the history cache and the merged library cache). No APPLY to Radarr.

Where it sits in the tree:
- **Parent**: `RadarrCacheManager` (`parent_name = "RadarrCacheManager"`).
- **Submanagers**: none.

Public methods:
- `get_recent_history(instance)` — reads `radarr.history.<instance>` (default `[]`).
- `refresh_history(instance, days_back=30)` — FETCH `GET history`; CACHE under `radarr.history.<instance>` (`compressed=True`). Note: `days_back` is accepted but never used in the request.
- `run_incremental_sync()` — iterates `config["radarr_instances"]` keys and calls `sync_from_history(instance)` for each.
- `sync_from_history(instance)` — the core incremental path (see below).
- `get_movie_watch_counts(instance)` — folds cached history into `{movieId: count}` by counting occurrences.

`sync_from_history(instance)` flow:
1. Looks up `config["radarr_instances"][instance]`; bails with an error if absent.
2. Builds cache key via `self.key_builder.format_cache_key("radarr/{instance}/library", instance=instance)` and loads it (`cached_data`), extracting `movies` and `meta.timestamp`.
3. If there is no cached timestamp → logs a warning and calls `self.manager.orchestration.run_movie_data_pull()` to regenerate the full cache, then returns.
4. Otherwise issues a RAW `requests.get` to `{base_url}/api/v3/history/since` with params `date=<cached_timestamp>`, `includeMovie=true`, `apikey=<instance_config["api"]>`. (This bypasses `radarr_api` and does NOT use a centralized client.)
5. Keeps history items whose `eventType` is in `{"downloadFolderImported", "movieFileRenamed", "movieAdded"}` and that carry a `movie`, then `global_cache.deduplicate_entries(cached_movies, new_movie_items, id_field="id")` to merge.
6. Writes the merged `{ "movies": ..., "meta": {"timestamp": <now UTC iso>} }` back to the library cache key and logs add/update/skip stats.

External API endpoints: `GET history` (via `radarr_api`); `GET /api/v3/history/since` (via raw `requests`).
Config keys read: `radarr_instances` (the map and per-instance `base_url`, `api`).
Global_cache keys: reads/writes `radarr/<instance>/library` (built via `key_builder.format_cache_key`); reads/writes `radarr.history.<instance>`.

`dry_run`: captured but unused; this manager only does GETs + cache writes (no Radarr mutation).

Singleton/concurrency: standard `BaseManager` singleton; sequential loop in `run_incremental_sync`.

## How it functions

`__init__` does BaseManager wiring, `self.register()`, then resolves `radarr_api`, `instance_manager`, `manager`, and `dry_run` from kwargs-or-parent. Entry point is `run_incremental_sync()`, which fans out to `sync_from_history(instance)`. The "no timestamp" branch delegates a full rebuild to the sibling orchestration manager's `run_movie_data_pull()`. Dedup/merge is delegated to `global_cache.deduplicate_entries`. No machine_learning delegation.

## Criteria & examples

- Event-type allow-list: only `downloadFolderImported`, `movieFileRenamed`, and `movieAdded` items contribute a movie to the merge. A `grabbed` event is ignored.
- Cold-start guard: if the library cache has no `meta.timestamp`, the manager does a full `run_movie_data_pull()` instead of an incremental fetch. Example: first ever run for `"default"` → warning logged, full pull triggered, no `history/since` call.
- Watch-count fold: cached history `[{"movieId": 7}, {"movieId": 7}, {"movieId": 9}]` → `get_movie_watch_counts` returns `{7: 2, 9: 1}`.

## In plain English

Instead of re-downloading the whole movie catalogue every time (slow, 20k entries), this manager remembers when it last looked and asks Radarr only "what changed since then?" — newly added films, imported downloads, renames — and folds just those into the saved catalogue. If it has never looked before, it just does the full download once. It can also tally how many times each movie shows up in the event log.

## Interactions

- **Parent**: `RadarrCacheManager`.
- **Siblings**: calls `self.manager.orchestration.run_movie_data_pull()` (the orchestration tree) on a cold start; shares the `radarr/<instance>/library` cache with the broader data-pull pipeline.
- **Services**: `radarr_api` for `GET history`; raw `requests` for `history/since`; `global_cache.deduplicate_entries` for merging.
- **Brain modules**: none.
