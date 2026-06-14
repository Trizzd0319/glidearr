# SonarrSyncFoldersManager

- **File** — `scripts/managers/services/sonarr/sync/folders.py`
- **One-liner** — Fetches, caches, and (when needed) creates Sonarr root folders for an instance, and maps a series path back to the root folder that contains it.

## What it does (for a senior Python engineer)

`SonarrSyncFoldersManager(BaseManager, ComponentManagerMixin)` is the root-folder authority for a Sonarr instance. It serves cached root-folder lists, resolves which root folder a given series path lives under, and ensures the per-instance root folders described in config actually exist in Sonarr.

Position in the manager tree:
- **Parent** — resolved from the class name: `parent_name` becomes `"SonarrSyncFolders"` (class name minus `"Manager"`; the literal default `"SonarrStorage"` is overwritten). Pulls `sonarr_api` / `logger` / `manager` from the registered parent if not injected.
- **Submanagers** — none (leaf).

FETCH / CACHE / APPLY:
- FETCH — Sonarr `rootfolder` (GET).
- CACHE — reads/writes the root-folder list under cache key `Paths.sonarr.SPACE_ESTIMATES` (= `"sonarr/<instance>/storage/space_estimates"`) via `global_cache.get_or_generate_cache`; clears it via `clear_cache`.
- APPLY — creates missing folders with Sonarr `rootfolder` POST (`payload={"path": path}`).

External API endpoints touched: Sonarr `rootfolder` (GET to list, POST to create).

Config keys read:
- `self.config.get("rootFolders", {})` — a `{name: base_path}` map of expected folders.
- `self.config.get_default_sonarr_instance_name()` (in `warm_cache`).

global_cache / Parquet keys:
- `Paths.sonarr.SPACE_ESTIMATES` (`sonarr/<instance>/storage/space_estimates`) — read in `get_root_folders`, cleared in `initialize_root_folders` and `clear_cached_root_folders`, warmed in `warm_cache`.

dry_run behavior: `self.dry_run` is captured in `__init__`, but the gate that actually matters is the explicit `dry_run` **argument** to `initialize_root_folders(instance, dry_run=False)`. When True, each missing folder is logged as `[DRY-RUN] Would create missing folder: <path>` and no POST is issued.

Singleton / concurrency: BaseManager singleton. No threading.

Public methods:
- `get_root_folders(instance)` → list — logs, then returns the cached/generated `rootfolder` list for the instance.
- `get_series_root_folder(series_path, instance)` → str | None — returns the first root-folder `path` that `series_path` starts with, else None.
- `initialize_root_folders(instance, dry_run=False)` — ensures every expected root folder exists; creates the missing ones (or logs them under dry-run).
- `clear_cached_root_folders(instance)` — clears the cache key `"{SPACE_ESTIMATES}.{instance}"`.
- `warm_cache(logger, cache, config)` — **staticmethod**; constructs a throwaway instance and preloads the root-folder cache with a 300s expiration.

## How it functions

`__init__` is the standard leaf pattern: BaseManager wiring, `register()`, parent/dep resolution, logger-required guard.

`get_root_folders` delegates to `global_cache.get_or_generate_cache(key=SPACE_ESTIMATES, generator_function=lambda: sonarr_api._make_request(instance, "rootfolder") or [])`. The generator returns `[]` on a falsy response so callers always get a list.

`initialize_root_folders`:
1. Clears the `SPACE_ESTIMATES` cache for the instance (so stale folder lists aren't trusted).
2. FETCHes current folders and normalizes existing paths to a lowercase, trailing-slash-stripped set.
3. Computes `expected_paths` as `{name: "<base_path>/<instance>".lower()}` from `config["rootFolders"]` — i.e. each configured base path gets a per-instance subfolder.
4. `missing_folders` = expected paths not in current paths.
5. If none missing, logs success and returns. Otherwise, for each missing folder either logs the dry-run line or POSTs `rootfolder` and logs ✅/❌.

`get_series_root_folder` does a simple `series_path.startswith(folder["path"])` scan.

`warm_cache` is a static convenience used by cache-warming routines; note it passes `manager.get_root_folders` as the generator **without** an `instance` argument, and uses a 300s `expiration_time`.

Brain delegation: none.

## Criteria & examples

- **Folder-exists test is case- and trailing-slash-insensitive.** Existing folder `/tv/Anime/` and expected `/tv/anime/sonarr-main` — existing normalizes to `/tv/anime`, expected to `/tv/anime/sonarr-main`; these differ, so the expected one is treated as missing and created.
- **Expected path shape.** With `config["rootFolders"] = {"Anime": "/tv/anime"}` and instance `sonarr-main`, the expected path is `/tv/anime/sonarr-main`.
- **Series-to-root resolution.** With root folders `["/tv/anime/sonarr-main", "/tv/series/sonarr-main"]`, a series at `/tv/anime/sonarr-main/Frieren` resolves to `/tv/anime/sonarr-main`; a series at `/movies/...` resolves to `None`.
- **Dry-run.** `initialize_root_folders("sonarr-main", dry_run=True)` with one missing folder logs `[DRY-RUN] Would create missing folder: /tv/anime/sonarr-main` and issues zero POSTs.

## In plain English

This is the manager that makes sure your TV server actually has the shelves it's supposed to have. Config says "there should be an Anime shelf and a Series shelf for this server"; this manager checks what shelves exist, and if the Anime shelf is missing it builds it (or, in a dry run, just says "I would build the Anime shelf here"). It also answers the question "which shelf is *Avatar: The Last Airbender* sitting on?" by matching the show's folder to the right shelf. And it remembers the shelf list for a few minutes so it doesn't have to re-ask the server constantly.

## Interactions

- **Parent** — `SonarrSyncManager` (registered as `SonarrSyncFolders`).
- **Sibling submanagers** — `SonarrSyncCustomFormatsManager`, `SonarrSyncMediaManager`, `SonarrSyncNamingManager`, `SonarrSyncTagsManager`.
- **Services** — Sonarr API (`rootfolder` GET/POST); `GlobalCacheManager` for the `SPACE_ESTIMATES` key.
- **Brain modules** — none.
