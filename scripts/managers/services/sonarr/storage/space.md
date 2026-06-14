# SonarrStorageSpaceManager

**File** — `scripts/managers/services/sonarr/storage/space.py`
**One-liner** — The disk-space measurement specialist: fetches and caches Sonarr root folders and diskspace, reports free/minimum free space per instance, and handles manual filesystem-total overrides.

## What it does (for a senior Python engineer)

`SonarrStorageSpaceManager(BaseManager, ComponentManagerMixin)` is the read-mostly "gas gauge" of the storage subtree. It owns everything to do with measuring capacity.

Key public methods:
- `get_free_space_per_instance()` — same logic as the parent's method: loops `config.get_sonarr_instances()`, resolves each name (string passthrough, else `self.instance_manager.resolve_name(instance)`), warms root folders, then `sonarr_api.disk_free_gb(...)` per instance, clamping `inf → 0.0`, rounding to 2 dp. Returns `{instance: free_gb}`. FETCH.
- `get_minimum_free_space()` — `min()` of the above (or `0`). FETCH.
- `get_root_folders(instance)` — CACHE-through via `self.cache_manager.get_or_generate_cache(...)`, key built from `CacheKeyPaths.sonarr.SPACE_ESTIMATES` (per-instance). Generator: `_fetch_root_folders`. FETCH+CACHE.
- `_fetch_root_folders(instance)` — `sonarr_api._make_request(instance, "rootfolder", fallback=[])`. Endpoint: Sonarr `rootfolder`.
- `run_storage_data_pull(instance)` — iterates **all** Sonarr API clients (`sonarr_api.get_all_sonarr_apis()`), and for each reads `sonarr_instances.<name>` from config to get `base_url` + `api`, then makes a **raw** `requests.get(f"{base_url}/api/v3/diskspace", headers={"X-Api-Key": ...})`. Serializes `path/label/freeSpace/totalSpace/unmappedFolders` and writes a `{"diskspace": [...], "meta": {...}}` envelope to the per-instance `SPACE_ESTIMATES` key via `cache_manager.set_with_pretty_output`. FETCH+CACHE. Note: the `instance` arg is ignored — it always sweeps every instance. Endpoint: Sonarr `GET /api/v3/diskspace`.
- `get_root_folder_map(instance)` — CACHE-through against key `f"sonarr/{instance}/storage"` using `sonarr_api.get_root_folders(...)` as generator. (Calls `self.resolve_instance` — provided by the parent, not this class.) FETCH+CACHE.
- `prompt_for_filesystem_total_if_missing(root_path) -> int` — interactive fallback when `shutil.disk_usage` can't size a volume. Matches `root_path` to a known instance via its root folders, `input()`-prompts the operator for a total size in GB, and upserts `{"path","totalSpace"}` into global_cache key `sonarr/manual_fs_total/<instance>`. Returns total bytes (or `0` on no-match/invalid input). CACHE (writes manual override).
- `warm_cache(logger, cache, config)` — `@staticmethod`; constructs a throwaway instance and pre-warms `SPACE_ESTIMATES` with a 300 s expiry.

Position in tree: a child of `SonarrStorageManager` (registry parent name `"SonarrStorage"`). Loads no submanagers of its own.

FETCH / CACHE / APPLY: FETCH (root folders, diskspace, free GB) and CACHE (`SPACE_ESTIMATES`, `sonarr/<instance>/storage`, `sonarr/manual_fs_total/<instance>`). No destructive APPLY.

Config keys read: `sonarr_instances` (full dict, and per-instance `base_url` / `api`), default Sonarr instance name.
Cache keys: `CacheKeyPaths.sonarr.SPACE_ESTIMATES` (per-instance; read via `cache_manager`, written by `run_storage_data_pull`), `sonarr/<instance>/storage` (via `global_cache`), `sonarr/manual_fs_total/<instance>` (via `global_cache`).
dry_run: captured (`kwargs["dry_run"]` else parent's, default `False`) but unused — none of its operations are destructive.
Threading/singleton: standard BaseManager singleton; `run_storage_data_pull` uses a bare `requests.get` (synchronous, no retry wrapper) rather than the shared `sonarr_api` client.

## How it functions

`__init__` derives `parent_name` from the class name (`"SonarrStorageSpaceManager"` → `"SonarrStorageSpace"`), calls `super().__init__` + `register()`, then looks the parent up from the registry and back-fills `sonarr_api`, `logger`, `manager`, `cache_manager`, `key_builder`, `dry_run`, `instance_manager` from kwargs-or-parent. It raises if no logger can be found.

Main control flows are independent read paths (no single `run()`):
1. `get_root_folders` → `_fetch_root_folders` is the canonical cached read used by most callers.
2. `run_storage_data_pull` is the bulk diskspace snapshotter that hits the raw `/diskspace` endpoint for every instance.
3. The manual-total path (`prompt_for_filesystem_total_if_missing`) exists for volumes `shutil.disk_usage` cannot size (e.g. some network mounts), persisting an operator-supplied total for later percent-free math done by `SonarrStorageLibraryManager`.

No `machine_learning` brain module is consulted; this is measurement only.

## Criteria & examples

- `inf → 0.0` clamp: an instance whose `disk_free_gb` is `inf` is recorded as `0.0`, so it sorts as "full" and never wins a "most free space" tiebreak.
- Manual total upsert: operator enters `4000` GB for `/tv` → stored as `4000 * 1024**3 = 4_294_967_296_000` bytes under `sonarr/manual_fs_total/<instance>`. If a prior entry for `/tv` exists, its `totalSpace` is overwritten in place; otherwise a new entry is appended.
- 300 s warm: `warm_cache` only regenerates `SPACE_ESTIMATES` if older than 300 seconds.

## In plain English

This is the fuel gauge and tape measure for your TV drives. It walks up to each storage drive, measures how much room is left, and writes it on a notepad (the cache) so nobody has to re-measure every five minutes. If a drive is the kind it can't read automatically (like a weird network share), it politely asks you "how big is this one?" and remembers your answer. It never deletes or moves anything — it just tells everyone else how full the closet is, the way you'd check how many more boxset DVDs can fit on the shelf before movie night.

## Interactions

- **Parent:** `SonarrStorageManager`.
- **Siblings:** consumed by `SonarrStorageSelectionManager` (free-space-based instance selection), `SonarrStorageLibraryManager` (percent-free / floor enforcement reads the manual-total cache it writes), and `SonarrStorageRelocationManager` (indirectly via selection).
- **Services touched:** Sonarr HTTP API (`rootfolder`, `GET /api/v3/diskspace`, `get_root_folders`, `disk_free_gb`); `global_cache` / `cache_manager`.
- **Brain modules:** none.
