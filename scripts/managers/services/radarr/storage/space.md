# RadarrStorageSpaceManager

- **File** — `scripts/managers/services/radarr/storage/space.py`
- **One-liner** — Measures available disk space per Radarr instance (mount-deduped) and caches each instance's root-folder list.

## What it does (for a senior Python engineer)

`RadarrStorageSpaceManager(BaseManager, ComponentManagerMixin)` is the `space` submanager under `RadarrStorageManager`. It is a thin FETCH/CACHE adapter — no deletions, no PUTs.

Key PUBLIC methods:
- `get_free_space_per_instance()` → `dict[resolved_instance, gb]`. Enumerates instances via `instance_manager.get_all_radarr_apis().keys()` (returns `{}` and warns if the instance manager is missing or has none). For each: resolves the name, primes its root folders, and reads `radarr_api.disk_free_gb(instance)` (mount-deduped: root folders sharing a physical disk are counted once). Clamps `inf` → `0.0`, rounds to 2 dp.
- `get_minimum_free_space()` → `float`. `min()` of the above (0 if empty). This is the value the space-pressure deletion loop keys off.
- `get_root_folders(instance)` → list of root-folder dicts. CACHE-through `global_cache.get_or_generate_cache(key=SPACE_ESTIMATES, generator_function=_fetch_root_folders)`.
- `_fetch_root_folders(instance)` → FETCH `radarr_api._make_request(instance, "rootfolder", fallback=[])`; returns `[]` if `radarr_api` is None.
- `_resolve_instance(instance)` — `instance_manager.resolve_instance` → `radarr_api.resolve_instance` → literal/`"default"`.
- `warm_cache(logger, cache, config)` — **staticmethod**; constructs a throwaway instance and primes `radarr/<instance>/storage/space_estimates` with `expiration_time=300`.

FETCH/CACHE/APPLY: **FETCH** (`rootfolder`, `disk_free_gb`) and **CACHE** (`SPACE_ESTIMATES`); no APPLY. `self.dry_run` is captured (from kwargs → parent → default False) but unused here — nothing mutates.

- External API endpoints: `GET rootfolder`; `radarr_api.disk_free_gb` (internal HTTP).
- Config keys: `config.get_default_radarr_instance_name()` (in `warm_cache`).
- global_cache keys: `CacheKeyPaths.radarr.SPACE_ESTIMATES` = `radarr/<instance>/storage/space_estimates` (read + write/generate).
- Singleton/concurrency: BaseManager singleton; self-registers; auto-links parent `RadarrStorageManager`.

## How it functions

`__init__` sets `parent_name = "RadarrStorageManager"`, calls `super().__init__` (dep injection + parent auto-link), `register()`, then pulls `radarr_api` / `instance_manager` / `manager` / `dry_run` from kwargs or the parent. There is no `load_components` call — this is a leaf manager with no children. Control flow at runtime: a caller (the space-pressure deletion loop, the selector, or the parent's `warm_cache`) calls `get_minimum_free_space()` → `get_free_space_per_instance()` → per-instance `get_root_folders()` (cache) + `disk_free_gb()` (live).

This file delegates no decision to a `machine_learning` brain — it only reports numbers. The space-pressure floor/band judgement (free_space_limit → floor T, band U=T×1.1) that consumes these numbers lives elsewhere in the deletion / brain layer.

## Criteria & examples

- **inf → 0.0 clamp**: an instance whose root folders are unreadable returns `inf` from `disk_free_gb`; recorded as `0.0`. So `{1080: 540.0, 4k: 0.0}` yields `get_minimum_free_space() == 0.0`, which (downstream) reads as maximal space pressure on that instance.
- **rounding**: `disk_free_gb = 23.456` → `23.46`.
- **empty guard**: no instance manager / no instances → returns `{}`, and `get_minimum_free_space()` returns `0`.

## In plain English

This is the clerk with the tape measure. Before the system decides whether there's room to keep adding movies (or whether it has to delete some to make space), this clerk walks each storage shelf and reports how many gigabytes are still free — counting a shared drive only once so the number isn't double-inflated. If a shelf can't be read at all, it conservatively reports "zero room left" so nothing gets dumped onto a broken shelf.

## Interactions

- **Parent**: `RadarrStorageManager`.
- **Siblings**: invoked by `RadarrStorageSelectorManager` (it constructs a space manager to pick an instance) and by `RadarrStorageDeletionManager`'s space-pressure path (consumes `get_minimum_free_space`).
- **Services**: `radarr_api` (`rootfolder`, `disk_free_gb`), `instance_manager` (`get_all_radarr_apis`, `resolve_instance`), `global_cache`.
