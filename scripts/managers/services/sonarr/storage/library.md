# SonarrStorageLibraryManager

**File** — `scripts/managers/services/sonarr/storage/library.py`
**One-liner** — The library catalogue + filesystem-floor specialist: read-only lookups over the cached Sonarr series library, percent-free computation per root folder, and an emergency free-space-floor enforcement that triggers deletion-based cleanup.

## What it does (for a senior Python engineer)

`SonarrStorageLibraryManager(BaseManager, ComponentManagerMixin)` serves two roles: (1) a cached series-library query API, and (2) the filesystem-floor watchdog that orchestrates corrective action when a root folder gets too full.

Lookup / query methods (all read the cached series library, CACHE reads):
- `get_series_cache(instance) -> dict` — loads `f"{CacheKeyPaths.sonarr.SONARR_LIBRARY}.{resolved_instance}"` from `global_cache` (default `{}`).
- `get_series_by_tvdb(tvdb_id, instance) -> dict|None` — linear scan matching `tvdbId`.
- `get_series_by_title(instance, title) -> dict|None` — case-insensitive title lookup. Prefers the **canonical** letter-bucketed cache (`SonarrCacheSeriesManager.get_series_by_title`, reached via `sonarr_cache.series` on self/parent, or registry `"SonarrCacheSeries"`); falls back to the legacy `SONARR_LIBRARY` dict scan.
- `is_series_in_library(tvdb_id, instance) -> bool`.
- `list_series_by_tag(tag, instance) -> list` — case-insensitive tag filter.
- `get_all_series_ids(instance) -> list[int]`.
- `get_title_by_series_id(series_id, instance) -> str|None`.
- `has_episode_file(series_id, season, episode, instance) -> bool` — checks key `f"{CacheKeyPaths.sonarr.EPISODE_FILE_MAP}.{instance}"` for composite key `f"{series_id}_{season}_{episode}"`.
- `is_pilot_episode(season, episode) -> bool` — `@staticmethod`, `season==1 and episode==1`.

Filesystem / floor methods:
- `record_filesystem_prompt(instance)` — reads root folders from `sonarr_api.get_root_folders`, sizes each with `shutil.disk_usage`; on failure `input()`-prompts the operator once for a total (GB) and reuses it. Writes `[{"path","totalSpace"}]` to `global_cache` key `sonarr/manual_fs_total/<instance>`. CACHE (write).
- `get_cached_total_space(instance, path) -> int|None` — reads back that manual-total cache.
- `compute_percent_free(path, instance) -> float|None` — `shutil.disk_usage(path).free/total*100`; on failure falls back to `free / cached_total * 100`; `None` if undeterminable.
- `get_critical_root_folder_status(instance, floor_percent=15.0)` — returns `[(path, percent_free), ...]` for folders under the floor.
- `enforce_free_space_floor(instance, floor_percent=15.0)` — the orchestrator. For each root folder, computes percent-free (with manual-total fallback, prompting and **recursing** once if no total is known). If any folder is below the floor it instantiates a local deletion manager and runs `deletion.delete_episodes_older_than(days=90)` then `deletion.delete_duplicate_episodes()`. Resolution relocation is **not** performed here — that is owned by the canonical `SonarrStorageRelocationManager.relocate_mismatched_resolutions` (driven by the orchestrator's `run_full_relocation`), which has make-before-break imports and file-id pilot guards. FETCH + APPLY (via children). (Note: this method is currently unwired — the live floor check runs through `SonarrStorageSpaceManager`.)
- `warm_cache(logger, cache, instance=None)` — `@staticmethod`; touches `f"{SONARR_LIBRARY}.{instance or 'default'}"`.

Position in tree: child of `SonarrStorageManager` (registry parent name `"SonarrStorage"`); also the object `SonarrStorageRelocationManager` adopts as its `selector` (it exposes `get_title_by_series_id` / `get_series_by_title`). Loads no submanagers; instantiates the deletion manager locally where needed.

FETCH / CACHE / APPLY: CACHE reads (series + episode-file maps), CACHE writes (manual FS totals), FETCH (`get_root_folders`), and APPLY indirectly (it never deletes/moves itself; it delegates to the deletion manager, and resolution relocation is owned by the canonical relocation manager).

Config keys read: none directly.
Cache keys: `CacheKeyPaths.sonarr.SONARR_LIBRARY.<instance>`, `CacheKeyPaths.sonarr.EPISODE_FILE_MAP.<instance>`, `sonarr/manual_fs_total/<instance>`.
API endpoints: Sonarr `rootfolder` (via `sonarr_api.get_root_folders`).
dry_run: captured from kwargs/parent (default `False`). Its destructive effects flow through the deletion/selection children, which apply their own dry-run gates.

## How it functions

`__init__` derives `parent_name` from the class name, calls `super().__init__` + `register()`, looks up the parent, back-fills `sonarr_api`/`logger`/`manager`/`dry_run`, and raises without a logger.

The two control flows of note:
1. **Query flow** — most methods just read the per-instance series cache and scan it; `get_series_by_title` adds a preference for the canonical letter-bucketed series cache before falling back to the legacy dict.
2. **Floor-enforcement flow** — `enforce_free_space_floor` is recursive: if a folder's total size is unknown and no manual total is cached, it calls `record_filesystem_prompt(...)` then re-invokes itself. Once any folder is confirmed below `floor_percent`, it spins up a local `SonarrStorageDeletionManager` and runs the 90-day expired-delete → duplicate-delete sequence. Resolution relocation is not part of this path — it lives in the canonical `SonarrStorageRelocationManager`.

No `machine_learning` brain module is consulted here. Thresholds (15% floor, 90-day delete) are local constants/arguments. (Under the ML migration, this "what to evict under pressure" decision is the kind intended to move into `machine_learning/`; this file currently hard-codes it.)

## Criteria & examples

- Free-space floor: default `15.0%`. A root folder at `12.4%` free is added to `below_floor_paths` and triggers cleanup; one at `18%` is left alone.
- Emergency cleanup ordering: when below floor, it deletes episodes older than **90 days**, then duplicates — in that fixed order. (Resolution relocation is a separate job owned by the canonical relocation manager.)
- Pilot guard: pilots are never relocated or deleted; the resolution-relocation pilot skip now lives in the canonical `SonarrStorageRelocationManager` (file-id aware, so it also covers omnibus / specials-shifted pilots), not in this module.
- Manual-total fallback: if `shutil.disk_usage("/mnt/tv")` throws and a cached total of `4 TB` exists for that path, percent-free = `free_bytes / 4_398_046_511_104 * 100`.

## In plain English

This is the librarian who also doubles as the fire marshal. As librarian, it answers questions instantly from its card catalogue — "do we have *Stranger Things*?", "what's the title for series #42?", "is there a copy of S02E05 on the 4K shelf?" — without walking the stacks, because it keeps a tidy index. As fire marshal, it watches how full each storage room is; the moment a room drops below 15% empty, it sounds the alarm and sends in the cleanup crew: first toss anything older than three months, then remove duplicate copies. (Moving shows that are sitting in the wrong-resolution room is a separate job, handled by the relocation specialist next door.) And like its colleagues, it never touches a show's pilot episode.

## Interactions

- **Parent:** `SonarrStorageManager`.
- **Siblings:** instantiates/uses `SonarrStorageDeletionManager` (deletes); is itself the `selector` for `SonarrStorageRelocationManager`, which owns resolution relocation and reads back `get_title_by_series_id` / `get_series_by_title` from this manager. May reach the canonical `SonarrCacheSeriesManager` (registry `"SonarrCacheSeries"`) for title lookups.
- **Services touched:** Sonarr HTTP API (`rootfolder`); local filesystem (`shutil.disk_usage`); `global_cache` for series/episode/FS-total keys.
- **Brain modules:** none directly (eviction policy is local; would be a candidate for `machine_learning/` under the ongoing brain migration).
