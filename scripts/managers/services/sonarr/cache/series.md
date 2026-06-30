# SonarrCacheSeriesManager

- **File** — `scripts/managers/services/sonarr/cache/series.py`
- **One-liner** — Stores and serves the entire Sonarr series library as letter-bucketed, gzip-compressed JSON files (`a.json.gz`, `b.json.gz`, …, `0–9.json.gz`, `_.json.gz`) with an in-memory memo, and exposes a rich set of read/lookup/map helpers over it.

## What it does (for a senior Python engineer)

`SonarrCacheSeriesManager(BaseManager, ComponentManagerMixin)` is reachable as `sonarr_cache.series`. Every method takes a **pre-resolved** instance-name string; callers resolve `None` → default before calling here.

Storage layout: files live at `{key_builder.base_dir}/sonarr/{instance}/library/{letter}.json.gz`. Paths are built **directly** from `key_builder.base_dir` (not via `build_cache_path`), because `CacheKeyBuilder.build_cache_path` appends its suffix to the key, which would mangle the compound `.json.gz` extension into `.json.gz.json`. The bucket letter for a title is its first alphanumeric character lower-cased; non-alphanumeric → `_`.

In-memory memo: `load_letter_cache` gunzips + JSON-parses a bucket on the first read of `(instance, letter)`, then serves a shared reference from `_bucket_memo_store` thereafter (callers must treat returned lists as read-only). Every write path keeps the memo in sync (`_bucket_memo_set`) or invalidates it (`_bucket_memo_invalidate`). This was added because full-library scans re-loaded the same ~40 buckets thousands of times per run.

Key public methods:
- `get_series_bucket_letter(title)` — the bucket letter for a title.
- `list_cached_letters(instance)` — sorted list of letters with a bucket file on disk.
- `clear_letter_cache(instance, letter)` — invalidate memo + delete one bucket file.
- `load_letter_cache(instance, letter)` — return one bucket's list (memoised).
- `save_series_to_letter_file(instance, series)` — upsert one series dict into its bucket (dedupe by `id`), write gzip, update memo.
- `rebuild_bucketed_series_cache(instance, all_series)` — full rebuild: group all series by letter, rewrite every bucket, log per-bucket progress.
- `delta_rebuild_series_cache(instance, live_series)` — smart delta sync: per bucket, compute added/removed/changed vs disk; rewrite only changed buckets, delete buckets with no live series. Returns `{"rewritten", "skipped", "added", "removed"}`. (Sonarr v3 has no delta endpoint, so `live_series` is the full library.)
- `get_all_series_ids(instance)` / `get_cached_series_by_id(instance, series_id)` — id set and single-series lookup (both scan all buckets).
- `deduplicate_series_data(existing, new_data)` — merge two lists by id → `(merged_dict, {"new","updated","skipped"})`.
- `persist_letter_cache(instance)` — verification step (writes are eager); logs bucket count + total entries.
- `summarize_cache_statistics(instance)` — per-bucket entry/missing-id counts + corrupt-file count.
- `iter_all_series(instance)` — generator over every cached series (preferred for whole-library scans).
- `get_all_series` / `get_series_count` / `get_all_titles` — flat list, count, title set.
- Single-series lookups: `get_series_by_title` (case-insensitive), `get_series_by_tvdb_id`, `get_title_by_series_id`, `is_series_in_library(instance, tvdb_id)`.
- Filtered reads: `get_monitored_series`, `get_unmonitored_series`, `get_series_by_status(status)`, `list_series_by_tag_id(tag_id)`.
- Map generators: `get_series_tags_map` (`{id: [tag_ids]}`), `get_series_quality_map` (`{id: qualityProfileId}`), `get_series_path_map`, `get_series_root_folder_map`.
- `remove_series(instance, series_id)` — find and remove one series from whichever bucket holds it; returns True/False.

FETCH / CACHE / APPLY: this is a pure **CACHE store/read** layer — no Sonarr HTTP calls and no `dry_run` gating (it is non-destructive local-disk I/O; even `delta_rebuild`/`remove_series`/`clear_letter_cache` write the local mirror unconditionally). Writes are gzip-JSON files passed through `make_json_safe`.

Config keys read: none. global_cache/Parquet keys: writes/reads the `*.json.gz` bucket files under `sonarr/{instance}/library/`.

## How it functions

Init wires `sonarr_cache`/`global_cache`/`manager` and registers. There is no `load_components` call (it has no submanagers). All real work is per-method file I/O: load via memo → mutate in memory → re-gzip → memo-sync. `rebuild_bucketed_series_cache` rewrites everything; `delta_rebuild_series_cache` is the I/O-minimal path used on normal syncs. No decision is delegated to a `machine_learning` module.

## Criteria & examples

- Bucketing: "The Mandalorian" → first alnum char `t` → bucket `t.json.gz`. "24" → `2.json.gz`. "[REC]" → first char `[` is non-alnum → bucket `_.json.gz`.
- Delta example: a bucket `b.json.gz` holds Breaking Bad + Bluey. A new sync adds "Bridgerton", removes nothing, and Bluey is unchanged → that bucket counts as `rewritten` (+1 added). A bucket whose three shows are byte-identical to disk is `skipped` (no write). A letter that has live shows on disk but none in `live_series` has its file `unlink()`-ed.

## In plain English

Imagine a library that files every TV show on a shelf by its first letter — all the "B" shows (Breaking Bad, Bluey, Bridgerton) go on the "B" shelf, and each shelf is kept in a sealed, shrink-wrapped box to save space. When the librarian needs a show, they open the box once and remember its contents so they do not keep unwrapping it. When the master list changes, they only re-pack the shelves that actually changed instead of redoing the whole library, which keeps the work fast even for a huge collection.

## Interactions

- **Parent manager:** `SonarrCacheManager` (attached as `series`).
- **Consumers:** `SonarrCacheEpisodeFilesManager` uses `series.iter_all_series` / `get_series_by_title` / `get_cached_series_by_id` heavily for show scoring and Tautulli matching; Tautulli sync uses `get_all_titles`.
- **Brain modules:** none called directly.
