# RadarrRepairMetadataManager

- **File** — `scripts/managers/services/radarr/repair/metadata.py`
- **One-liner** — Detects and repairs movie-metadata problems in Radarr (missing tmdbId/imdbId, problematic title characters, year mismatches) by re-fetching records and batching `RefreshMovie`, with a 24-hour pending-refresh cache to avoid re-queuing.

## What it does (for a senior Python engineer)

`RadarrRepairMetadataManager` is a `BaseManager` + `ComponentManagerMixin` loaded by `RadarrRepairWrapperManager` under the key `metadata`. `parent_name` derives from the class name (`RadarrRepairMetadata`). Deps (`radarr_api`, `instance_manager`, `dry_run`) from kwargs-or-parent.

Module constants:
- `_PROBLEMATIC_CHARS_RE = re.compile(r'[\\*?"<>|]')` — characters genuinely bad for filesystem/API use. Colon and forward-slash are deliberately excluded (legit in titles like "Mission: Impossible", "AC/DC").
- `_PENDING_TTL_S = 86_400` — 24h TTL for the pending-refresh set.

- **FETCH / CACHE / APPLY.** All three:
  - FETCH: `_get_movies` (prefers global_cache `radarr.movies.{instance}.full`, else `GET movie`), and `GET movie/{id}` for individual re-fetch.
  - CACHE: read/write the pending-refresh set under `radarr/metadata/pending_refresh/{instance}` (a sorted list of movie ids), with a 24h `expiration_time` (falls back to a plain `set` call if the cache impl rejects the kwarg).
  - APPLY: `POST command` `RefreshMovie` (batched).
- **External API endpoints:** `movie` (GET, via cache fallback), `movie/{id}` (GET individual), `command` (POST `RefreshMovie`).
- **Config keys.** None read.
- **global_cache / Parquet keys.** Reads/writes `radarr/metadata/pending_refresh/{instance}`; reads `radarr.movies.{instance}.full`.
- **dry_run.** Repair methods log `"[dry_run] Would RefreshMovie …"` and count as queued without POSTing; in dry_run the individual-fetch path still runs (it only logs the resolved-id message when not dry_run).
- **Singleton / concurrency.** `BaseManager` singleton. RefreshMovie is async server-side; the pending-set is the de-dupe mechanism across runs. Batched in groups of 50.

Public methods:

- `find_missing_external_ids(instance) -> list[dict]` — FETCH-only. Returns `{movie_id, title, year, tmdb_id, missing_ids}` for movies missing `tmdbId` and/or `imdbId`.
- `repair_missing_ids(instance) -> stats` — APPLY. Skips movies already in the pending set, then splits into two buckets: (1) *missing imdbId only* → re-fetch `GET movie/{id}` (the bulk list sometimes omits imdbId that the detail endpoint has); if still absent, fall into the refresh bucket. (2) *missing tmdbId* → straight to the refresh bucket. Resolved imdb ids are cleared from the pending set; the refresh bucket is `RefreshMovie`-batched (50) and queued ids are added to the pending set. Returns `{checked, imdb_resolved, refresh_queued, still_missing, failed}`.
- `find_problematic_titles(instance) -> list[dict]` — FETCH-only. Returns `{movie_id, title, year, problematic_chars}` for titles matching `_PROBLEMATIC_CHARS_RE`.
- `repair_problematic_titles(instance) -> stats` — APPLY. Skips pending movies, then `RefreshMovie`-batches (50) the rest so Radarr re-pulls canonical titles; adds queued ids to the pending set. Returns `{checked, refresh_queued, failed}`. (Docstring mentions a 1s pause but the code adds no sleep — central serialisation handles contention.)
- `find_year_mismatches(instance, tolerance=1) -> list[dict]` — FETCH-only. Compares Radarr `year` to a `(YYYY)` extracted from the movie file's `relativePath`/`path`; flags when `abs(diff) > tolerance`. Returns `{movie_id, title, radarr_year, path_year, path}`.
- `repair_refresh_metadata(instance, movie_ids=None) -> stats` — APPLY (legacy, per-movie). RefreshMovie for an explicit id list (or derived from `find_missing_external_ids`); no pending-set integration. Returns `{checked, triggered, failed}`.
- `run(instance) -> dict` — The pass invoked by the wrapper. Returns `{"missing_ids_repair": repair_missing_ids(...), "problematic_titles_repair": repair_problematic_titles(...), "year_mismatches": find_year_mismatches(...)}`. NOTE: `run` performs the two ID/title repairs (subject to dry_run) but only *reports* year mismatches.

Internal helpers: `_resolve_instance`, `_get_movies`, `_pending_cache_key`, `_get_pending_ids`, `_add_pending_ids`, `_clear_resolved_ids`.

## How it functions

Lifecycle: `__init__` → `register()` → resolve deps → debug log. No children loaded.

The defining mechanism is the **pending-refresh cache**: because `RefreshMovie` is asynchronous and slow server-side, every queued id is stamped into `radarr/metadata/pending_refresh/{instance}` with a 24h TTL, and subsequent runs skip those ids until the cache expires — preventing the same movies being re-queued every run. Resolved imdb ids are explicitly removed from the set. All policy is local (the regex, the bucket split, the batch size); no `machine_learning` delegation.

## Criteria & examples

- **Problematic-title regex excludes `:` and `/`:** "Mission: Impossible" is NOT flagged; "What*Ever" (asterisk) IS flagged with `problematic_chars=['*']`.
- **Year mismatch with tolerance 1:** Radarr year 2005, path `Title (2005)/file.mkv` → 0 diff, fine. Radarr year 2005, path `Title (2007)/…` → diff 2 (> 1) → flagged `{radarr_year:2005, path_year:2007}`. A diff of exactly 1 is allowed.
- **imdb-only resolution path:** a movie with tmdbId present but imdbId missing → `GET movie/{id}`; if the detail record has `imdbId`, it counts as `imdb_resolved` and is cleared from pending (no refresh). If still missing → goes into the RefreshMovie batch.
- **Pending skip:** if 30 missing-ID movies were queued last run and are still within 24h, a re-run logs "Skipping 30 …" and only processes the remainder.

## In plain English

Movie info sometimes goes bad — a movie has no TMDb/IMDb id, a title has illegal characters like `*` or `?`, or the year on the label doesn't match the year in the folder name. This manager spots those and asks Radarr to refresh the movie from the internet database to fix them. Because that refresh takes a while in the background, it keeps a 24-hour note ("already asked for these") so it doesn't keep nagging Radarr about the same movies on every run. For a movie just missing its IMDb id, it first peeks at the full record — often the id is already there and no refresh is even needed.

## Interactions

- **Parent manager** — `RadarrRepairWrapperManager` (loads it as `metadata`).
- **Sibling submanagers** — None invoked.
- **Brain modules** — None.
- **Other services** — `radarr_api` (movie, movie/{id}, command); `instance_manager` for resolution; `global_cache` for the pending-refresh set and the movie cache.
