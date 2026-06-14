# RadarrSyncNamingManager

**File** — `scripts/managers/services/radarr/sync/naming.py`
**One-liner** — Reads and (when permitted) pushes Radarr file/folder naming configuration to every instance, lightly sanitizing the format strings while preserving Radarr's naming tokens.

## What it does (for a senior Python engineer)

`RadarrSyncNamingManager(BaseManager, ComponentManagerMixin)` is a leaf service manager under `RadarrSyncManager`. It performs FETCH (GET config/naming) and APPLY (PUT config/naming). It does no caching. No `load_components`; no submanagers.

Public methods:
- `sanitize_naming_format(fmt) -> str` — returns `fmt.strip()` if truthy, else `fmt` unchanged. Intentionally minimal so Radarr tokens like `{Movie Title}` / `{Quality Full}` are preserved verbatim.
- `get_naming_config(instance) -> dict` — FETCH. GET `config/naming` (fallback `{}`).
- `sync_naming_settings(naming_config)` — APPLY. Copies the supplied config, trims the `standardMovieFormat` and `movieFolderFormat` fields, then PUTs the result to every configured instance. Under `dry_run` logs a "would apply" line and PUTs nothing. Each instance's PUT is wrapped in try/except. Returns early if no instances are configured.

Helper:
- `_resolve_instance(instance)` — `instance_manager.resolve_instance` → `radarr_api.resolve_instance` → `instance or "default"`.

External API endpoints (via `radarr_api._make_request`): `config/naming` (GET, PUT).
Config keys read: `radarr_instances` (its keys are the target instance list in `sync_naming_settings`).
global_cache keys: none.
dry_run: gates the PUT in `sync_naming_settings`.
Concurrency/singleton: standard `BaseManager` singleton; no threading.

## How it functions

Lifecycle: `__init__` injects shared deps, sets `parent_name="RadarrSyncManager"`, calls `register()`, captures `radarr_api`/`instance_manager`/`dry_run`.

`sync_naming_settings` control flow: `config = naming_config.copy()` → for each of `["standardMovieFormat", "movieFolderFormat"]` present in the copy, replace with its stripped value and log a debug line if it actually changed → read `config["radarr_instances"]` keys → for each instance, PUT the cleaned config (or log a dry-run line). Note the caller passes the naming config in; this manager does not derive it from anywhere (typically it is read from one instance via `get_naming_config` and broadcast).

No decision is delegated to a `machine_learning` brain module.

## Criteria & examples

- **Trimming only the two format fields.** Given `naming_config = {"standardMovieFormat": "  {Movie Title} ({Release Year}) ", "renameMovies": True}`, the PUT payload's `standardMovieFormat` becomes `"{Movie Title} ({Release Year})"` (leading/trailing spaces removed) while `renameMovies` is untouched and inner tokens/spaces are preserved.
- **No-change debug line suppressed.** If `movieFolderFormat` is already `"{Movie Title} ({Release Year})"` (no surrounding whitespace), stripping is a no-op and no "Cleaned naming format field" debug line is logged.
- **Empty instance list.** If `config["radarr_instances"]` is empty, it logs "No Radarr instances configured for naming sync." and returns without any PUT.

## In plain English

Radarr lets you decide how downloaded movie files and folders are named — for example "Movie Title (Year)" so The Princess Bride lands as `The Princess Bride (1987)`. If you run several storerooms, you want them all to name files the same way. This manager takes one naming template and applies it everywhere, just tidying up any stray spaces at the very start or end of the template (it carefully leaves the `{...}` placeholders alone, since those are Radarr's instructions). In pretend mode it simply says "I would apply this naming template here" without changing anything.

## Interactions

- **Parent:** `RadarrSyncManager`.
- **Siblings:** `RadarrSyncCustomFormatsManager`, `RadarrSyncFoldersManager`, `RadarrSyncMediaManager`, `RadarrSyncTagsManager`.
- **Services:** `radarr_api` (`RadarrInstanceManager`) for HTTP; `instance_manager` for resolution; reads `radarr_instances` config for the target list.
- **Brain modules:** none.
