# RadarrRepairOrphansManager

- **File** — `scripts/managers/services/radarr/repair/orphans.py`
- **One-liner** — Detects and repairs orphaned/stale Radarr state: movies Radarr thinks are missing (rescan + refresh them) and on-disk folders Radarr never imported (trigger an import scan).

## What it does (for a senior Python engineer)

`RadarrRepairOrphansManager` is a `BaseManager` + `ComponentManagerMixin` loaded by `RadarrRepairWrapperManager` under the key `orphans`. `parent_name` derives from the class name (`RadarrRepairOrphans`). Deps (`radarr_api`, `instance_manager`, `dry_run`) from kwargs-or-parent.

- **FETCH / CACHE / APPLY.**
  - FETCH: paginated `GET wanted/missing`, `GET rootfolder`, plus `_get_movies` which **prefers global_cache** (`radarr.movies.{instance}.full`) before falling back to `GET movie`.
  - APPLY: `POST command` with `RescanMovie`, `RefreshMovie`, and `DownloadedMoviesScan`.
  - CACHE: reads the movie cache; does not write any cache.
- **External API endpoints:** `wanted/missing?pageSize=&page=` (GET), `rootfolder` (GET), `command` (POST: `RescanMovie`, `RefreshMovie`, `DownloadedMoviesScan`), `movie` (GET, fallback).
- **Config keys.** None read.
- **global_cache / Parquet keys.** Reads `radarr.movies.{instance}.full` (via `_get_movies`, though note the main paths here use `wanted/missing` and `rootfolder` directly). Writes none.
- **dry_run.** All repair methods log `"[dry_run] Would …"` and count as queued/triggered without issuing the POST.
- **Singleton / concurrency.** `BaseManager` singleton. Commands are batched (size 100 for stale-record repair); the code comments note that SQLITE_BUSY retry/backoff and per-instance write serialisation are handled centrally inside `_make_request`, so no inter-batch sleeps are added here.

Public methods:

- `find_stale_records(instance) -> list[dict]` — FETCH-only. Paginates ALL pages of `wanted/missing` (pageSize 250), returning monitored records as `{movie_id, title, year, path, tmdb_id}`. Pagination stops when `page * page_size >= totalRecords` or a page is empty.
- `repair_stale_records(instance) -> stats` — APPLY, two passes over the stale ids in batches of 100: Pass 1 `RescanMovie` (re-detect files Radarr lost track of), Pass 2 `RefreshMovie` (re-pull TMDb metadata, clear stuck states). On a failed batch (`_make_request` returns the `None` fallback) it counts `failed += len(batch)`. Returns `{checked, rescan_queued, refresh_queued, failed}`.
- `find_untracked_files(instance) -> list[dict]` — FETCH-only. Reads each root folder's `unmappedFolders` from `GET rootfolder`; returns `{root_folder, folder_path, folder_name}` per unmapped folder.
- `repair_import_untracked(instance) -> stats` — APPLY. For every untracked folder, triggers `DownloadedMoviesScan` with that path. Returns `{checked, triggered, failed}`.
- `repair_trigger_import(instance, folder_path) -> stats` — APPLY (legacy single-path). Triggers `DownloadedMoviesScan` for one explicit folder. Returns `{triggered, failed}`.
- `repair_rescan_all(instance) -> stats` — APPLY. Triggers a `RescanMovie` command with no filter (full library). Returns `{triggered, failed}`.
- `run(instance) -> dict` — The pass invoked by the wrapper. Returns `{"stale_records": repair_stale_records(...), "untracked_import": repair_import_untracked(...)}`. NOTE: unlike the tags/quality scans, `run` here DOES perform repairs (rescan/refresh + import scan), subject to dry_run.

Internal helpers: `_resolve_instance`, `_get_movies` (cache-preferring movie fetch).

## How it functions

Lifecycle: `__init__` → `register()` → resolve deps → debug log. No children loaded.

`run()` chains two repairs: re-detect/refresh stale records, then scan-in untracked folders. Both rely on `_make_request` returning the fallback (`None`) instead of raising on failure, so batch failures are counted rather than aborting the run. All decisions are mechanical (monitored-and-missing → rescan/refresh; unmapped folder → import scan); the in-code note says the *triage* of whether to ultimately search or unmonitor is left to the anomaly manager's logic on a later run. No `machine_learning` delegation here.

## Criteria & examples

- **Stale record:** a record in `wanted/missing` with `monitored=True` is collected. Example: 1,200 monitored-missing movies → paginated in pages of 250 (5 pages) → 1,200 ids → RescanMovie in 12 batches of 100, then RefreshMovie in 12 batches.
- **Batch failure accounting:** if one `RescanMovie` batch of 100 returns the `None` fallback, `failed += 100` and a warning is logged; the remaining batches still run.
- **Untracked folder:** a root folder `/movies` reporting an `unmappedFolders` entry `/movies/Tenet (2020)` → one `DownloadedMoviesScan` with that path. In dry_run it logs `"Would scan untracked folder: '/movies/Tenet (2020)'"`.

## In plain English

Two clean-up jobs in one. First: Radarr sometimes thinks a movie is missing even though the file is sitting right there on disk (or its info got stale). This manager taps Radarr on the shoulder — "look again" (rescan), then "refresh that movie's details" — so the library reflects reality. Second: sometimes a movie folder shows up on disk that Radarr never noticed; this manager points Radarr at that folder and says "import this." In preview mode it announces both jobs without actually poking Radarr.

## Interactions

- **Parent manager** — `RadarrRepairWrapperManager` (loads it as `orphans`).
- **Sibling submanagers** — Hands follow-up work to `RadarrRepairAnomalyManager` indirectly: refreshed records get triaged (search vs unmonitor) on a later anomaly run.
- **Brain modules** — None.
- **Other services** — `radarr_api` (wanted/missing, rootfolder, command, movie); `instance_manager` for resolution; `global_cache` for the optional movie cache.
