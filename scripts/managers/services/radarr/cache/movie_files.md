# RadarrCacheMovieFilesManager

- **File** — `scripts/managers/services/radarr/cache/movie_files.py`
- **One-liner** — The Radarr movie-file ledger: builds a flat, ML-ready `movie_files.parquet` per instance (identity + tech specs + ratings + watch stats + lifecycle/decision columns), applies grace-period deletion marking, and (as a fallback) deletes grace-expired files — with hard franchise/keep/universe guards.

## What it does (for a senior Python engineer)

`RadarrCacheMovieFilesManager(BaseManager, ComponentManagerMixin)` is the heaviest member of the cache tree and the closest thing here to a full FETCH→CACHE→APPLY adapter:
- **FETCH**: GET `movie` (or `radarr.movies.<instance>.full` from cache), GET `moviefile?movieId=<id>` per-movie fallback, GET `tag`, GET `qualityprofile`, plus Tautulli watch history via `TautulliAPI.get_history`.
- **CACHE**: writes `{base_dir}/radarr/<instance>/movie_files.parquet` (Snappy/pyarrow), defined by `SCHEMA_COLUMNS` (~80 columns across Identity, Classification, People, Studio, Release, Ratings, File/Technical, Lifecycle, Radarr metadata, and a Decision ledger).
- **APPLY**: `DELETE moviefile/<fid>` for grace-expired, unguarded, watched files.

Where it sits in the tree:
- **Parent**: `RadarrCacheManager` (note `parent_name` is set to the class-name-minus-"Manager" string, like the relational sibling; actual construction parent is `RadarrCacheManager`).
- **Submanagers**: none.

Key public methods:
- `run(instance)` — full lifecycle: `refresh(persist=False)` → `apply_grace_period(df=self._refreshed_df)` → conditionally `delete_marked_files`. Returns merged stats.
- `refresh(instance, persist=True)` — FETCH + build all Parquet rows. Reuses cached movie/tag/profile data when present, fetches per-movie files when `movieFile` is absent, fetches Tautulli watch map, resolves keep-policy/universe/franchise maps, builds one row per movie WITH a file, coerces numeric columns, stashes the frame on `self._refreshed_df`, and (if `persist`) saves. Returns `{"movies","with_file","rows_built","saved"}`.
- `apply_grace_period(instance, df=None)` — sets/clears `marked_for_deletion` + `available_until` per row via the brain's grace decision; NEVER marks franchise/keep/universe; resets prior space-pressure `planned_action` stamps; saves (even in dry_run, since marks are local cache state).
- `delete_marked_files(instance)` — iterates marked rows, applies layered guards, then DELETEs (or "would delete" in dry_run); returns a rich stats dict with per-guard skip counts and `bytes_freed`.
- `load(instance)` / `save(instance, df)` — Parquet read (with numeric coercion) / write (sorted by title, year).
- `get_summary(instance)` — totals: rows, watched, franchise entries, universe movies, pending quality actions, total size GB, codec/resolution distributions, HDR count.

Constants: `CACHE_MAX_AGE = 172_800` (48h), `GRACE_HOURS = 24`, `DELETION_MIN_FREE_GB = 25.0`.

Internal helpers: `_resolve_instance`, `_parquet_path`, `_safe_concat` (all-NA-safe concat), `_fmt_bytes`, `_extract_people` (crew/cast → pipe-separated columns, top-10 cast by `order`), `_extract_media_info` (mediaInfo → tech columns), `_build_row`, `_fetch_watch_map` (Tautulli), `_get_free_space_gb` (delegates to `radarr_api.disk_free_gb`).

External API endpoints: `GET movie`, `GET moviefile?movieId=<id>`, `GET tag`, `GET qualityprofile`, `DELETE moviefile/<fid>`; Tautulli `get_history(length=5000)`.
Config keys read: `tautulli` (instance configs), `free_space_limit`, `grace_window_ramp`, and `radarr_instances` indirectly via instance resolution.
Global_cache keys read: `radarr.movies.<instance>.full`, `radarr.tags.<instance>`, `radarr.quality.<instance>`. Parquet written: `radarr/<instance>/movie_files.parquet`.

`dry_run` (strict): resolved from kwargs → parent `manager.dry_run` → `RadarrManager.dry_run` → `Main.dry_run`. If STILL unresolved, `__init__` **raises `ValueError`** — it refuses to initialize without an explicit value, to prevent accidental destructive deletes. The Parquet itself materialises even in dry_run (it is a GET-only mirror); only `DELETE moviefile/<fid>` is gated.

Singleton/concurrency: standard `BaseManager` singleton; pandas operations are row-iterated (not threaded). `radarr_api` is hardened the same way as the relational sibling (kwargs/parent, drop if no `_make_request`, registry fallback to `RadarrManager`).

## How it functions

`run(instance)` control flow:
1. `refresh(instance, persist=False)` builds the frame in memory and stashes it on `self._refreshed_df` (avoids a redundant refresh-save + grace-save).
2. `apply_grace_period(instance, df=self._refreshed_df)` writes the frame once with grace marks applied.
3. Reads `free_gb = _get_free_space_gb(instance)`.
4. If `config.free_space_limit > 0`: it ONLY marks and delegates — logs that deletion is owned by the space-pressure target loop, and returns refresh stats (no blanket delete).
5. Else (legacy, no floor configured): if `free_gb >= DELETION_MIN_FREE_GB` (25 GB) it skips deletion; otherwise it calls `delete_marked_files`.

Brain delegation (machine_learning modules — named only, not documented here):
- `classification.franchise.build_franchise_file_ids` (via `_build_franchise_file_ids`) and `classification.franchise.resolve_franchise_entries` (via `_resolve_franchise_entries`).
- `classification.keep_policy.build_keep_policy_map` (via `_build_keep_policy_map`) — produces the keep-policy + universe-name maps.
- `lifecycle.grace_policy.movie_grace_decision`, `grace_mark`, and `grace_window_multiplier` — drive the per-row grace decision and the optional score-scaled grace window in `apply_grace_period`.

`apply_grace_period` per-row logic: reads guard signals (`is_franchise_entry`, `movie_file_id` ∈ franchise file ids, `keep_policy` ∈ {keep_forever, keep_movie, universe}, `is_watched`, `last_watched_at`), then calls `movie_grace_decision(...)`. `"clear"` → force `marked_for_deletion=False`; `"skip"` → leave as-is; otherwise compute the deletion window `grace_td` (× `grace_window_multiplier(percentile, grace_window_ramp)` when configured) and call `grace_mark(...)` to set `available_until` + `marked_for_deletion`. It then RESETS any prior `planned_action ∈ {delete, downgrade}` ledger stamps so the dry-run ledger doesn't report stale plans (the real deleter/downgrader re-stamps its current selection).

`delete_marked_files` guard order (defence-in-depth): (1) hard franchise-entry guard, (2) franchise-file-id guard, (3) universe guard (`keep_policy == "universe"`), (4) keep guard (`keep_forever`/`keep_movie`), (5) missing-`movie_file_id` skip. Surviving rows are DELETEd (or logged as "would delete" with a reason in dry_run).

## Criteria & examples

- **Grace window**: `GRACE_HOURS = 24`. A watched movie with `last_watched_at` 2026-06-01 gets `available_until` ≈ 2026-06-02; once `now` passes that, it is eligible for deletion. With a configured `grace_window_ramp`, a high-percentile favourite gets a longer window and a low-percentile film a shorter one (multiplier defaults to exactly 1.0 → fixed 24h when ramp is `{}`).
- **Never-mark rule**: a franchise entry (`is_franchise_entry=True`), or any `keep_policy` in {keep_forever, keep_movie, universe}, is forced `marked_for_deletion=False` regardless of watch state. Example: the first film in a collection (the franchise entry) is never marked even if watched and stale.
- **Deletion gate**: with no `free_space_limit` set, deletion only runs when `free_gb < 25.0`. Example: 30 GB free → grace-period deletion skipped; 12 GB free → `delete_marked_files` runs. With `free_space_limit > 0`, this manager never blanket-deletes — the space-pressure loop owns it.
- **Universe guard at delete time**: a marked row whose `keep_policy == "universe"` is unmarked at deletion with a `UNIVERSE GUARD` warning and counted in `skipped_universe`; universe films are downgraded/upgraded, never deleted here.
- **Row inclusion**: `refresh` only emits rows for movies with `hasFile` truthy (and a resolvable `movieFile`).

## In plain English

This is the master spreadsheet of every movie file you own — not just the title, but the resolution, codec, audio, ratings, who's in it, and crucially how often you've actually watched it (pulled from your Plex/Tautulli history). Once a movie has been watched, a 24-hour "last chance" clock starts; when it runs out the file becomes a candidate for cleanup to free disk space. But there are unbreakable rules: the first movie in a series (your gateway into, say, the Marvel saga) is never deleted, and anything you've tagged "keep" or marked as part of a cinematic universe is protected too. When you're just testing (dry-run), it writes down exactly what it WOULD delete and why, without touching a single file.

## Interactions

- **Parent**: `RadarrCacheManager`.
- **Siblings**: `RadarrCacheRelationalManager` (consumes this manager's `movie_files` DataFrame via `build_from_movie_files_df`); reuses `radarr.tags.<instance>` (`RadarrTagCacheManager`) and `radarr.movies.<instance>.full` from the data-pull pipeline. Deletion below the floor is deliberately delegated to the Radarr `quality/` space-pressure manager.
- **Services**: `radarr_api` (movie/file/tag/profile GETs, `DELETE moviefile`, `disk_free_gb`); `TautulliAPI` (watch history); `global_cache.key_builder` (Parquet path); pandas/pyarrow.
- **Brain modules**: `classification.franchise`, `classification.keep_policy`, `lifecycle.grace_policy` (delegated decisions only; not documented here).
