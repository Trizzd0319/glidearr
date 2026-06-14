# RadarrStorageDeletionManager

- **File** — `scripts/managers/services/radarr/storage/deletion.py`
- **One-liner** — The Radarr movie-file deletion executor: applies grace periods and performs deletions (Parquet-backed lifecycle plus legacy age-based and duplicate cleanup), always gated by `dry_run` and franchise protection.

## What it does (for a senior Python engineer)

`RadarrStorageDeletionManager(BaseManager, ComponentManagerMixin)` is the `deletion` submanager under `RadarrStorageManager`. This is the one storage leaf that actually issues destructive `DELETE`s, so it has the strictest `dry_run` resolution.

There are two families of methods:

**Parquet-backed lifecycle (delegating to `RadarrCacheMovieFilesManager`):**
- `apply_grace_period(instance) -> dict`. Resolves the instance, looks up `RadarrCacheMovieFilesManager` via `_get_movie_files_manager()`; if absent returns `{}`. Otherwise calls `movie_files_mgr.apply_grace_period(instance)`, counts the `marked_for_deletion` column on the returned DataFrame, logs, and returns `{"marked": N}`.
- `delete_marked_movies(instance) -> dict`. Resolves the instance and delegates to `movie_files_mgr.delete_marked_files(instance)` (which is where franchise protection + keep-policy + the actual API deletes live). Returns `{}` if the movie-files manager is unavailable.
- `get_movies_pending_deletion(instance) -> list[dict]`. Read-only preview: `movie_files_mgr.load(instance)`, filters `marked_for_deletion`, and projects `{movie_id, movie_file_id, title, year, size_bytes, available_until, keep_policy}` per row. No deletion.

**Legacy direct deletion (talks to `radarr_api` itself):**
- `delete_movies_older_than(instance, days=30)`. FETCH `GET moviefile`; for each file whose `dateAdded` (ISO, `Z` stripped) is older than `now - days`, APPLY `DELETE moviefile/{id}` — unless `self.dry_run`, in which case it logs `"[dry_run] Would delete ..."`. Uses a `tqdm` progress bar on stderr.
- `delete_duplicate_movies(instance)`. FETCH `GET moviefile`; groups by `movieId`, sorts each group by quality id descending, keeps the highest-quality file, and `DELETE`s the rest (or logs in dry_run).

Helpers: `_resolve_instance` (instance_manager → radarr_api → literal/`"default"`); `_fmt_bytes(n)` static human-readable size; `_get_movie_files_manager()` → `registry.get("manager", "RadarrCacheMovieFilesManager")` or `None`.

FETCH/CACHE/APPLY: **FETCH** (`GET moviefile`) and **APPLY** (`DELETE moviefile/{id}`); CACHE/Parquet access is delegated to `RadarrCacheMovieFilesManager`.

- External API endpoints: `GET moviefile`, `DELETE moviefile/{id}`.
- Config keys: none read directly (the destructive-default guard pulls `dry_run` indirectly from config-derived values held on `RadarrManager` / `Main`).
- Parquet keys: indirect — the `movie_files` Parquet, owned by `RadarrCacheMovieFilesManager`; columns referenced: `marked_for_deletion`, `movie_id`, `movie_file_id`, `title`, `year`, `size_bytes`, `available_until`, `keep_policy`.
- Singleton/concurrency: BaseManager singleton; self-registers; auto-links parent. `parent_name` overwritten in `__init__` to `"RadarrStorageDeletion"`.

## How it functions

`__init__` performs a **fail-closed `dry_run` resolution chain**: it tries `kwargs["dry_run"]`, then `parent.dry_run`, then `registry.get("manager","RadarrManager").dry_run`, then `registry.get("manager","Main").dry_run`. If all are `None` it **raises `ValueError`** rather than default to False — deliberately refusing to initialize without an explicit value, to prevent accidental destructive operations. Only after a non-None value is found does it set `self.dry_run = bool(_dry_run)`.

This addresses the documented BaseManager footgun where `dry_run` silently defaults to False if not captured per-leaf — here a missing value is an error, not a silent unsafe default.

**Delegated decisions:** the value-judgement of *which* marked files survive (franchise protection, keep-policy, whole-file delete guards) is NOT in this file — it lives in `RadarrCacheMovieFilesManager` (which in turn consumes the `machine_learning` brain's grace/space-pressure plan). This manager is the thin APPLY adapter; the brain decides, the Parquet manager marks, this manager (or that manager's `delete_marked_files`) fires the DELETEs.

The module-level invariant noted in the docstring — "FRANCHISE ENTRIES ARE NEVER DELETED" — is enforced in the delegated `apply_grace_period` / `delete_marked_files` path, not by code visible in this file.

## Criteria & examples

- **dry_run gate**: with `dry_run=True`, `delete_movies_older_than("1080", days=30)` logs `[dry_run] Would delete movie file ID 512 ...` for each stale file and the `deleted` counter stays 0 — nothing is removed.
- **Age cutoff**: with `days=30` run on 2026-06-10, a file with `dateAdded = "2026-05-01T..."` (40 days old) is past the cutoff → deletion candidate; a file added `2026-05-25` (16 days) is kept. Files with missing or unparseable `dateAdded` are skipped.
- **Duplicate retention**: for `movieId=27` with two files of quality id 7 (1080p) and 3 (720p), the id-7 file is retained and the id-3 file is deleted (highest quality id wins).
- **Grace-period count**: if `movie_files_mgr.apply_grace_period("4k")` returns a DataFrame with 4 rows where `marked_for_deletion` is truthy, `apply_grace_period` returns `{"marked": 4}`.

## In plain English

This is the clerk who actually throws old movies away — but only after a strict double-check. Before deleting anything it demands a clear yes/no on whether this is a real run or just a rehearsal ("dry run"); if nobody will tell it, it refuses to start rather than risk binning something by accident. It never deletes anything from a franchise you're collecting (so your full *Lord of the Rings* set is safe even if one film hasn't been watched in a year), it tidies up duplicate copies by keeping only the best-quality version, and it can clear out films that have sat unwatched past their grace period. The hard thinking about *what* deserves to go is done by a separate "brain"; this clerk just carries out the verdict.

## Interactions

- **Parent**: `RadarrStorageManager` (and, for the `dry_run` fallback chain, `RadarrManager` then `Main`).
- **Siblings**: peers in the storage cluster.
- **Delegates to**: `RadarrCacheMovieFilesManager` (resolved from the registry) for the Parquet-backed grace/keep/franchise/whole-file-delete logic, which itself consumes the `machine_learning` brain's grace and space-pressure plans (brain not documented here).
- **Services**: `radarr_api` (`moviefile` GET/DELETE), `instance_manager` (resolution), `registry` (locating the movie-files manager and the dry_run authorities).
