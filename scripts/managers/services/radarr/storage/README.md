# RadarrStorageManager

- **File** — `scripts/managers/services/radarr/storage/__init__.py`
- **One-liner** — The storage sub-tree orchestrator for Radarr: it loads the five storage submanagers (space, library, selector, deletion, relocation) and exposes free-space / root-folder convenience methods.

## What it does (for a senior Python engineer)

`RadarrStorageManager(BaseManager, ComponentManagerMixin)` is the parent of the Radarr "storage" cluster. Its `__init__` does not use the mixin's `load_components` helper; instead it builds an `init_kwargs` dict (shared deps + `radarr_api`, `instance_manager`, `manager=self`, `dry_run`, `key_builder`) and hand-rolls the component loading so it can split criticality:

- It declares `all_component_classes = {deletion, library, selector, space, relocation}` and `critical_keys = {space, library, selector, deletion, relocation}` (i.e. **all five are critical**).
- It calls `split_components(...)` (from `support/utilities/managers/component_splitter.py`) to partition the dict by criticality / parent-name match, then instantiates each class directly, `setattr`s it onto `self` under its short name, and sets a registry flag `radarr.storage.<name>_initialized` (True/False). A failure of any critical component flips `all_critical_loaded` to False.
- After loading, it sets `self.all_components_loaded` and the registry flag `radarr.storage_manager_initialized`, then logs one filtered summary line via `log_filtered_component_summary(service_name="Radarr", ...)`.

Key PUBLIC methods (this class also re-implements space helpers that overlap with `RadarrStorageSpaceManager`):
- `get_free_space_per_instance()` → `dict[instance, gb]`. Iterates `config.get_radarr_instances()`, resolves each instance, fetches its root folders, and asks `radarr_api.disk_free_gb(instance)` for mount-deduped free space. Clamps `inf` (no roots / unreadable) to `0.0` and rounds to 2 dp. Returns `{}` if no instances configured.
- `get_minimum_free_space()` → `float`. The `min()` of the above values (0 if empty).
- `get_root_folders(instance)` → list of root-folder dicts. CACHE-through: `global_cache.get_or_generate_cache(key=..., generator_function=_fetch_root_folders)`.
- `_fetch_root_folders(instance)` → FETCH: `radarr_api._make_request(instance, "rootfolder", fallback=[])`.
- `_resolve_instance(instance)` — resolves via `instance_manager.resolve_instance`, then `radarr_api.resolve_instance`, then falls back to the literal or `"default"`.
- `warm_cache(logger, cache, config)` — **staticmethod**. Spins up a throwaway `RadarrStorageSpaceManager` and primes the `radarr/<instance>/storage/space_estimates` key with a 300 s expiry.

FETCH/CACHE/APPLY: this manager performs **FETCH** (`rootfolder`, `disk_free_gb`) and **CACHE** (space-estimates key); it performs no APPLY (no PUT/DELETE/POST). dry_run is captured into `self.dry_run` and threaded into children but is not itself acted on here.

- External API endpoints touched: `GET rootfolder` (via `_make_request`), plus whatever `radarr_api.disk_free_gb` calls internally.
- Config keys read: `config.get_radarr_instances()`, `config.get_default_radarr_instance_name()` (in `warm_cache`).
- global_cache keys read/written: `CacheKeyPaths.radarr.SPACE_ESTIMATES` = `radarr/<instance>/storage/space_estimates`.
- Singleton/concurrency: `BaseManager` is a process-wide singleton keyed by `(class, singleton_key)`; this manager self-registers under the registry "manager" category and auto-links to its parent (`RadarrManager`).

## How it functions

Lifecycle: `__init__` → `super().__init__` (BaseManager injects logger/config/global_cache/validator/registry and auto-links parent) → `register()` → build `init_kwargs` → `split_components` → instantiate critical then non-critical components, recording per-component load status in `self.load_summary` and registry flags → set aggregate flags → emit one summary line.

Notable internal helpers: `_resolve_instance`, `_fetch_root_folders`. There is overlap with `RadarrStorageSpaceManager` (both expose `get_free_space_per_instance` / `get_minimum_free_space` / `get_root_folders`); this parent uses `config.get_radarr_instances()` to enumerate, whereas the space submanager enumerates via `instance_manager.get_all_radarr_apis()`.

No decision is delegated to a `machine_learning` brain module from this file directly; the value judgements (grace, deletion, scoring) live downstream in the deletion path's `RadarrCacheMovieFilesManager`.

## Criteria & examples

- **inf-clamp rule**: if `disk_free_gb` returns `float("inf")` (e.g. an instance with no readable root folders), the value is recorded as `0.0`. Example: instance `4k` returns `inf` → stored `0.0`, so `get_minimum_free_space()` across `{1080: 812.5, 4k: 0.0}` returns `0.0` — a misconfigured instance correctly reads as "no space" and dominates the minimum.
- **rounding rule**: `disk_free_gb=812.4567` → stored `812.46`.

## In plain English

Think of this as the warehouse manager for your 4K / 1080p / 720p movie "shelves." It hires five specialist clerks (one to measure free shelf space, one who keeps the catalogue, one who decides which shelf a new film goes on, one who throws out old films, and one who moves films between shelves) and makes sure they all share the same notebook and walkie-talkie. It can also answer quick questions like "how much room is left on the emptiest shelf?" before anyone buys a new Blu-ray of, say, *The Princess Bride*.

## Interactions

- **Parent manager**: `RadarrManager` (auto-linked; also consulted for `dry_run` resolution by the deletion child).
- **Sibling submanagers it loads**: `RadarrStorageSpaceManager` (`space`), `RadarrLibraryCacheManager` (`library`), `RadarrStorageSelectorManager` (`selector`), `RadarrStorageDeletionManager` (`deletion`), `RadarrStorageRelocationManager` (`relocation`).
- **Services/util it talks to**: `radarr_api` (HTTP), `instance_manager` (instance-name resolution / API map), `global_cache` (`CacheKeyBuilder`), `RegistryManager` (flags), `split_components` util.
