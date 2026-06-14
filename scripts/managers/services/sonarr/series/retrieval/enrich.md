# SonarrSeriesRetrievalEnrichManager

- **File** — `scripts/managers/services/sonarr/series/retrieval/enrich.py`
- **One-liner** — Builds the enriched series DataFrame: it pulls every Sonarr series, attaches TVDB metadata in parallel, and writes the result to a per-instance Parquet file.

## What it does (for a senior Python engineer)

`SonarrSeriesRetrievalEnrichManager(BaseManager, ComponentManagerMixin)` joins the raw Sonarr series list with TVDB metadata into a single pandas DataFrame and persists it as `library.enriched.parquet`. It is the bridge between "Sonarr says we have this show" and "here is everything we know about it."

**Position in the manager tree**
- Loaded by `SonarrSeriesRetrievalManager` as the `enrich` component.
- `parent_name = "SonarrSeries"`.
- Deps off `kwargs["manager"]`: `sonarr_cache`, `global_cache`, `sonarr_api`, `tvdb` (the `SonarrSeriesRetrievalTVDBManager`), `series_cache`, and `dry_run` (read from the parent only).

**FETCH / CACHE / APPLY** — FETCH (Sonarr series list via arrapi; TVDB metadata via the tvdb manager) + CACHE (writes the enriched Parquet; the tvdb manager handles its own `tvdb/*.json` caching). No Sonarr APPLY.

**External API endpoints** — indirect: `sonarr_api.get_all_sonarr_apis().get(instance)` yields an arrapi object whose `.all_series()` returns the library; TVDB is reached through `self.tvdb.fetch_tvdb_data(...)`.

**Public method**
- `build_enriched_series_dataframe(instance, save=True, save_unenriched=True) -> pd.DataFrame` — the entry point. Pulls series, builds base rows, enriches those with a `tvdb_id` in parallel (8 threads), assembles a DataFrame, optionally saves Parquet + an unenriched log, logs an enrichment summary, returns the DataFrame.

**Internal helpers**
- `_extract_series_base_row(series, instance, tag_map) -> dict` — flattens an arrapi series object into a base row: `instance`, `series_id`, `title`, `slug`, `path`, `status`, `language` (name), `is_monitored`, `year`, `tvdb_id`, `tags` (resolved to label names), `season_folder`, `runtime`, `genres`, `added`, `last_info_sync`, `quality_profile_id`, `season_count` (len of seasons).
- `_resolve_tag_name(tag_id, tag_map)` — maps a tag id to its label, or `UnknownTag-{id}`.
- `_get_output_path(instance) -> Path` — `{cache_root}/sonarr/{instance}/library.enriched.parquet`, where `cache_root` comes from `sonarr_cache.cache_root` (falling back to `global_cache.cache_root`).

**Config keys** — none read directly.
**Cache keys** — reads the tag list from `sonarr/{instance}/tags.json` (via `sonarr_cache.get`); writes the Parquet `sonarr/{instance}/library.enriched.parquet`; relies on the tvdb manager's `tvdb/*.json` cache.
**dry_run** — captured from the parent but **not** branched on; `build_enriched_series_dataframe` writes the Parquet whenever `save=True`. (This is a cache/dataset write, not a Sonarr APPLY, so it does not mutate the external service.)
**Concurrency** — uses `concurrent.futures.ThreadPoolExecutor(max_workers=8)` to fan out TVDB enrichment, with a `tqdm` progress bar. It also creates a `multiprocessing.get_context("spawn").Lock()` to guard the shared `enriched` / `unenriched` lists during the threaded `enrich()` closure (note: a multiprocessing lock used inside a thread pool — it serializes the list appends).

## How it functions

Lifecycle: `BaseManager` init → dep resolution → register.

`build_enriched_series_dataframe` control flow:
1. Resolve the arrapi for `instance`; if none → log error, return empty DataFrame.
2. Load tag data from `sonarr/{instance}/tags.json`, build `tag_map = {id: label}`.
3. `arrapi.all_series()`; if empty → log error, return empty DataFrame.
4. Partition: for each series build a base row via `_extract_series_base_row`; rows **with** a `tvdb_id` go to `tvdb_queue` (to be enriched), rows **without** go straight to `enriched`.
5. Define `enrich(entry)`: call `self.tvdb.fetch_tvdb_data(tvdb_id=..., fallback_title=...)`; if data, merge into the base row and append to `enriched` (under lock); else append to `unenriched` (under lock).
6. Run `enrich` across `tvdb_queue` via the 8-worker pool, wrapped in `tqdm`.
7. `df = pd.DataFrame(enriched)`. If `save`, write to `_get_output_path` as Parquet. If `save_unenriched` and there are unenriched rows, call `logger.log_unenriched_series(instance, unenriched)`. Then `logger.log_enrichment_summary(df, unenriched)`. Return `df`.

No `machine_learning` brain module is involved — this is data assembly, not a value judgement. (The enriched Parquet it produces is later consumed by downstream scoring/decision layers.)

## Criteria & examples

- **Enrich-vs-skip partition rule:** a series is enriched only if its base row has a truthy `tvdb_id`. Example: a show with `tvdbId=305288` is queued for TVDB lookup; a show Sonarr has with no `tvdbId` is dropped straight into the DataFrame as-is (no TVDB call).
- **Unenriched fallthrough:** if `fetch_tvdb_data` returns `{}` (e.g. token expired, or TVDB has no record), that row goes to `unenriched` and is logged but is **not** in the returned DataFrame.
- **Tag resolution:** tag id `7` with `tag_map = {7: "anime"}` resolves to `"anime"`; an id not in the map becomes `"UnknownTag-7"`.
- **Parallelism:** with `max_workers=8`, up to eight TVDB lookups run concurrently; appends to the shared lists are serialized by the lock.

## In plain English

Sonarr gives you a bare list — "we have show #1234, it's at this folder, it's monitored." That's not much to work with. This worker takes each show on the list and, for every one that has a TVDB number, sends a researcher to fetch the full fact sheet (genres, network, seasons, year). To go fast it sends eight researchers at once. It then staples each show's bare info to its fact sheet and files everything into one big spreadsheet (a Parquet file) the rest of the app reads from. Shows it couldn't find a fact sheet for are noted on a separate "couldn't enrich" list so nothing silently vanishes.

## Interactions

- **Parent manager:** `SonarrSeriesRetrievalManager`.
- **Siblings:** calls the `tvdb` manager (`SonarrSeriesRetrievalTVDBManager`) for per-series metadata; the enriched Parquet it writes is the source the `cache` manager's `rebuild_individual_series_caches` can rebuild buckets from.
- **Services:** `sonarr_api` (arrapi `all_series()`), `sonarr_cache` (tag list + cache root for the output path), `global_cache` (cache-root fallback). Specialized logger hooks `log_unenriched_series` / `log_enrichment_summary`.
- **Brain modules:** none directly; it produces the enriched dataset that downstream decision layers consume.
