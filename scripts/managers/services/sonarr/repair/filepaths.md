# SonarrRepairFilepathsManager

**File** — `scripts/managers/services/sonarr/repair/filepaths.py`
**One-liner** — Verifies Sonarr root-folder mappings exist on disk, finds and (optionally) deletes orphaned series folders, and purges cache entries for series Sonarr no longer knows about.

## What it does (for a senior Python engineer)

`SonarrRepairFilepathsManager(BaseManager, ComponentManagerMixin)` is a leaf repair sub-manager under `SonarrRepairManager`. It touches both the Sonarr API and the local **filesystem**, and performs **FETCH**, **CACHE** (rewrite library cache), and **APPLY** (delete folders / root folders).

- **Parent:** `self.parent_name = "SonarrRepair"`. Constructed by `SonarrRepairManager` and listed in its `critical_keys`.
- **Deps:** `sonarr_api` resolved from `sonarr_api`/`api` kwargs or the parent's `api` attr (raises `ValueError` if unresolved). `dry_run`, `auto_repair`, `rebuild_metadata` are read off the `manager` kwarg (defaulting `False`). The destructive operations require **both** `auto_repair` and `not dry_run`.
- **Loads submanagers:** none.

Public methods:

- **`repair_root_folder_mappings()`** — iterates every instance in `sonarr_api.get_all_sonarr_apis()`, lists `api.get_root_folders()`, and for each folder checks `Path(path).exists()`. Missing folders are warned; if `auto_repair and not dry_run`, it removes the broken root folder via `api.delete_root_folder(folder_id)`.
- **`cleanup_orphaned_folders()`** — for each instance, fetches `api.all_series()`. **Safety abort:** if `all_series()` is empty it skips that instance entirely (treats empty as a likely API failure rather than deleting everything). Otherwise it builds the set of valid resolved series paths, iterates each root folder's direct children, and any subdirectory not in the valid-paths set is flagged orphaned. If `auto_repair and not dry_run`, it recursively unlinks files and removes directories, then `rmdir`s the orphan folder.
- **`purge_orphaned_cache_keys()`** — for each instance, reads the library cache at `CacheKeyPaths.sonarr.LIBRARY` (formatted with `instance=instance_name`), gets live series IDs via `api.all_series()`, keeps only cached `series` entries whose `id` is still live, and (if `not dry_run`) writes the trimmed `{"series": valid_entries}` back to the same cache key.

- API endpoints touched: `get_root_folders`, `delete_root_folder`, `all_series`.
- Config keys read: none directly.
- global_cache keys: reads/writes `sonarr/<instance>/library` (`CacheKeyPaths.sonarr.LIBRARY`).
- FETCH / CACHE / APPLY: all three.
- dry_run: gates every destructive action; cache rewrite is also skipped under dry-run.
- Singleton/threading: standard `BaseManager` singleton; no threading.

## How it functions

Lifecycle: `__init__` calls `super().__init__`, `self.register()`, sets `parent_name`, resolves `sonarr_api` and the three behavior flags from the `manager` kwarg, and raises if no API. Each public method loops over instances and combines API reads with `pathlib` filesystem inspection. The orphan-folder method is the riskiest (it deletes from disk) and is intentionally guarded by the empty-`all_series` abort plus the `auto_repair`/`dry_run` double gate. No `machine_learning` brain module is involved.

## Criteria & examples

- **Missing root folder:** `Path(folder.path).exists()` is `False`. Example: root `/data/tv` no longer exists; with `auto_repair=True, dry_run=False` it calls `api.delete_root_folder(folder_id)` and logs removal. With `dry_run=True` it only warns.
- **Orphaned folder:** a directory under a root whose resolved path is not in `{resolved(series.path)}`. Example: `/data/tv/Old Show (2009)` exists on disk but no series points there → flagged; deleted only if `auto_repair and not dry_run`.
- **Empty-library safety abort:** if `api.all_series()` returns `[]`, the instance is skipped with `🛑 Aborting orphan folder cleanup … No folders will be deleted.` — this prevents wiping the whole library when the API silently fails.
- **Orphaned cache entry:** cached series with `id=42` where `42` is not a live series ID is dropped. Example: cache holds 100 series, 3 IDs are gone live → `removed = 3`, trimmed list written back (unless dry-run).

## In plain English

This is the shop's storeroom auditor. First it checks every shelving unit (root folder) actually still exists in the building — if one is gone, it can remove the now-pointless label. Then it walks the storeroom looking for show boxes sitting on the floor that aren't on any official inventory list, and (only if you've explicitly turned on "auto-tidy" and you're not in practice mode) it throws those stray boxes out. Crucially, if the official inventory list comes back completely blank, it assumes something's broken and refuses to throw anything away — better safe than deleting your whole library. Finally it crosses out catalog entries for boxes that are no longer there.

## Interactions

- **Parent manager:** `SonarrRepairManager`.
- **Siblings:** the other `SonarrRepair*Manager` specialists.
- **Services:** the Sonarr per-instance API clients (`sonarr_api`), the local filesystem (`pathlib`), and `global_cache` (library key).
- **Brain modules:** none.
