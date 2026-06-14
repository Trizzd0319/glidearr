# RadarrSyncMediaManager

**File** — `scripts/managers/services/radarr/sync/media_management.py`
**One-liner** — Reads and (when permitted) pushes Radarr media-management settings, metadata config, and quality profiles + custom formats so that all instances share one canonical configuration.

## What it does (for a senior Python engineer)

`RadarrSyncMediaManager(BaseManager, ComponentManagerMixin)` is a leaf service manager under `RadarrSyncManager`. It performs FETCH (GET config/mediamanagement, metadata, qualityprofile, customformat), CACHE (per-instance metadata list), and APPLY (PUT media-management settings; POST quality profiles + custom formats). No `load_components`; no submanagers.

Public methods:
- `get_media_management_settings(instance) -> dict` — FETCH. GET `config/mediamanagement` (fallback `{}`); not cached.
- `sync_media_management_settings(settings)` — APPLY. For each instance: GET current `config/mediamanagement`, `dict.update(settings)` (so it merges, not replaces), then PUT it back. Skips an instance whose current settings aren't a dict; under `dry_run` logs a "would sync" line and PUTs nothing. Each instance is wrapped in try/except so one failure doesn't abort the rest.
- `get_metadata(instance) -> list` — FETCH + CACHE. Served from `global_cache` key `radarr.metadata.<resolved>` if present, else GET `metadata` and cache it.
- `sync_quality_across_instances()` — APPLY. Treats the first instance as the reference, reads its `qualityprofile` and `customformat` lists, and POSTs each of them to every other instance. Under `dry_run` logs a count-only "would sync" line per target instance.

Helpers:
- `_resolve_instance(instance)` — `instance_manager.resolve_instance` → `radarr_api.resolve_instance` → `instance or "default"`.
- `_get_all_instances()` — keys of `radarr_api.get_all_radarr_apis()`; on failure falls back to `config["radarr_instances"]` keys.

External API endpoints (via `radarr_api._make_request`): `config/mediamanagement` (GET, PUT), `metadata` (GET), `qualityprofile` (GET, POST), `customformat` (GET, POST).
Config keys read: `radarr_instances` (only as the fallback instance source in `_get_all_instances`).
global_cache keys: `radarr.metadata.<resolved_instance>` (read/written in `get_metadata` only).
dry_run: gates the PUT in `sync_media_management_settings` and the POSTs in `sync_quality_across_instances`.
Concurrency/singleton: standard `BaseManager` singleton; no threading.

## How it functions

Lifecycle: `__init__` injects shared deps, sets `parent_name="RadarrSyncManager"`, calls `register()`, captures `radarr_api`/`instance_manager`/`dry_run`.

`sync_media_management_settings` is a *merge-and-push*: it never blindly overwrites an instance's media-management block — it fetches the live block, overlays the caller-supplied `settings` keys, and PUTs the merged result, so unspecified fields are preserved.

`sync_quality_across_instances` is a *broadcast from a reference*: `all_instances[0]` is the source of truth; instances `[1:]` receive its profiles and formats via POST. (Each profile/format is POSTed individually; there is no dedup or conflict check here — that fuzzy logic lives in `RadarrSyncCustomFormatsManager`.)

No decision is delegated to a `machine_learning` brain module.

## Criteria & examples

- **Merge semantics.** If an instance's current media-management config is `{"recycleBin": "/recycle", "minimumFreeSpaceWhenImporting": 100}` and you call `sync_media_management_settings({"minimumFreeSpaceWhenImporting": 250})`, the PUT payload becomes `{"recycleBin": "/recycle", "minimumFreeSpaceWhenImporting": 250}` — only the supplied key changes.
- **Structure guard.** If a GET returns a non-dict (e.g. an error list) for an instance, that instance is logged as "Invalid media management structure" and skipped, but the loop continues to the others.
- **Reference broadcast.** With instances `["main", "4k", "kids"]`, `sync_quality_across_instances` reads profiles/formats from `main` and POSTs them to `4k` and `kids`. Under dry_run it logs e.g. `[dry_run] Would sync 5 profiles and 12 formats to 4k`.

## In plain English

This is the "house style" enforcer for your movie storerooms. Some settings decide how Radarr handles files — where the recycle bin is, how much free space to keep, what counts as acceptable quality (your "I'll accept 1080p but prefer 4K" preferences). This manager can take one storeroom as the gold standard and copy its quality rulebook to all the others, and it can patch shared file-handling settings everywhere at once without clobbering the settings you didn't mention. So your 4K shelf and your kids shelf end up following the same playbook as your main shelf — and in pretend mode it just tells you "I'd copy 5 rulebooks and 12 format rules over."

## Interactions

- **Parent:** `RadarrSyncManager`.
- **Siblings:** `RadarrSyncCustomFormatsManager`, `RadarrSyncFoldersManager`, `RadarrSyncNamingManager`, `RadarrSyncTagsManager` (notably overlaps `RadarrSyncCustomFormatsManager`, which owns the deduplicated/fuzzy format sync; this manager does a simpler reference-broadcast).
- **Services:** `radarr_api` (`RadarrInstanceManager`) for HTTP; `instance_manager` for resolution; `global_cache` for the per-instance metadata cache; `radarr_instances` config as a fallback instance source.
- **Brain modules:** none.
