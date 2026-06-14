# RadarrSyncFoldersManager

**File** — `scripts/managers/services/radarr/sync/folders.py`
**One-liner** — Reads, caches, and (when permitted) provisions Radarr root-folder configuration, ensuring each instance has the root folders implied by the app's `rootFolders` config.

## What it does (for a senior Python engineer)

`RadarrSyncFoldersManager(BaseManager, ComponentManagerMixin)` is a leaf service manager under `RadarrSyncManager`. It performs FETCH (GET root folders), CACHE (per-instance root-folder list), and APPLY (POST a missing root folder). No `load_components`; no submanagers.

Public methods:
- `get_root_folders(instance) -> list` — FETCH + CACHE. Returns the instance's root-folder list (`[{path, ...}]`); served from `global_cache` key `radarr.rootfolders.<resolved>` if present, else GET `rootfolder` and cache it.
- `get_movie_root_folder(movie_path, instance)` — given an absolute movie path, returns the root-folder `path` it lives under (first folder whose `path` is a `startswith` prefix), else `None`.
- `clear_cached_root_folders(instance)` — sets the cache key to `None` (invalidate) and logs it.
- `initialize_root_folders(instance)` — APPLY. Invalidates the cache, GETs current folders, computes the set of expected paths from config, and POSTs any missing ones. Under `dry_run` it logs a "would create" line per missing folder and creates nothing.

Helper:
- `_resolve_instance(instance)` — `instance_manager.resolve_instance` → `radarr_api.resolve_instance` → `instance or "default"`.

External API endpoints (via `radarr_api._make_request`): `rootfolder` (GET, POST).
Config keys read: `rootFolders` (a `{name: base_path}` map). Expected paths are built as `f"{base_path.rstrip('/')}/{resolved}".lower()` — i.e. each base path gets the resolved instance name appended as a subfolder.
global_cache keys: `radarr.rootfolders.<resolved_instance>` (read/written in `get_root_folders`; invalidated in `clear_cached_root_folders` and at the top of `initialize_root_folders`; the POSTs in `initialize_root_folders` do **not** re-warm it, so a follow-up read re-fetches).
dry_run: gates only the POST in `initialize_root_folders`.
Concurrency/singleton: standard `BaseManager` singleton; no threading.

## How it functions

Lifecycle: `__init__` injects shared deps, sets `parent_name="RadarrSyncManager"`, calls `register()`, captures `radarr_api`/`instance_manager`/`dry_run`.

`initialize_root_folders` control flow: invalidate cache → GET current folders → normalize current paths to a lowercased, trailing-slash-stripped set → build `expected_paths` from `config["rootFolders"]` → compute `missing_folders` as expected paths not present → if none, log and return → else POST `{"path": path}` for each missing one (or log a dry-run line). Path comparison uses lowercase + `rstrip("/")` on the *current* side and `lower()` on the expected side.

No decision is delegated to a `machine_learning` brain module.

## Criteria & examples

- **Missing-folder detection.** With `config["rootFolders"] = {"movies": "/data/media"}` and resolved instance `main`, the expected path is `/data/media/main`. If the instance currently reports only `/data/media/4k`, then `/data/media/main` is missing and gets a `POST rootfolder {"path": "/data/media/main"}` (or `[dry_run] Would create missing folder: /data/media/main`).
- **Prefix match in `get_movie_root_folder`.** With folders `[{path: "/data/media/main"}]`, calling `get_movie_root_folder("/data/media/main/Inception (2010)", "main")` returns `/data/media/main`; a path under `/mnt/other/...` returns `None`.
- **No-op case.** If every expected path is already present, it logs "All required root folders already exist." and creates nothing.

## In plain English

Picture a warehouse where every movie has to go in a specific aisle. This manager checks each storeroom (Radarr instance) and confirms the right aisles physically exist — and if an expected aisle is missing, it builds it (or, in pretend mode, just says "I would build aisle /data/media/main"). It can also answer "which aisle does this particular film sit in?" by matching the start of the film's shelf address. So when Radarr later downloads, say, a Pixar movie, there's guaranteed to be a labelled aisle waiting for it instead of the download failing for lack of a home.

## Interactions

- **Parent:** `RadarrSyncManager`.
- **Siblings:** `RadarrSyncCustomFormatsManager`, `RadarrSyncMediaManager`, `RadarrSyncNamingManager`, `RadarrSyncTagsManager`.
- **Services:** `radarr_api` (`RadarrInstanceManager`) for HTTP; `instance_manager` for resolution; `global_cache` for the per-instance root-folder cache; reads the app's `rootFolders` config.
- **Brain modules:** none.
