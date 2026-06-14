# SonarrStorageManager

**File** — `scripts/managers/services/sonarr/storage/__init__.py`
**One-liner** — Top-level orchestrator for Sonarr disk/storage concerns; it loads and wires the storage submanagers (space, library, selection) and exposes a small free-space reporting surface of its own.

## What it does (for a senior Python engineer)

`SonarrStorageManager(BaseManager, ComponentManagerMixin)` is the parent node for the `scripts/managers/services/sonarr/storage/` subtree. It is the manager that each storage submanager looks up under registry name `"SonarrStorage"` (their `parent_name`), even though this class's own `parent_name` attribute is set to `"SonarrStorageManager"`.

Responsibilities:
- Construct the storage submanagers and attach each as an attribute (`self.library`, `self.selection`, `self.space`).
- Resolve and propagate shared dependencies (logger, config, two cache handles, validator, registry, `sonarr_api`, a `CacheKeyBuilder`, and `dry_run`) into every submanager.
- Provide a thin FETCH/CACHE surface for free space and root folders.

Key public methods:
- `get_free_space_per_instance()` — iterates `config.get_sonarr_instances()`, resolves each via `self.resolve_instance(...)` (a method inherited from / provided by the parent Sonarr service manager, not defined here), fetches root folders, then calls `self.sonarr_api.disk_free_gb(resolved_instance)`. Returns `{instance: free_gb}` rounded to 2 dp. `inf` (no roots / unreadable mount) is clamped to `0.0` so downstream `min()`/selection logic stays well-behaved. With the collapse to a single `sonarr` instance this map now holds a single value, so the per-instance/minimum surface is effectively one free-space number. FETCH.
- `get_minimum_free_space()` — returns `min(...)` of the per-instance map (or `0` when empty). Used as the system-wide free-space signal.
- `get_root_folders(instance)` — CACHE-through read: builds the cache key from `CacheKeyPaths.sonarr.SPACE_ESTIMATES` and serves via `global_cache.get_or_generate_cache(...)`, generating with `_fetch_root_folders`. FETCH+CACHE.
- `_fetch_root_folders(instance)` — internal generator: `sonarr_api._make_request(instance, "rootfolder", fallback=[])`. FETCH. Endpoint: Sonarr `rootfolder`.
- `warm_cache(logger, cache, config)` — `@staticmethod`; constructs a throwaway `SonarrStorageSpaceManager` and pre-populates `CacheKeyPaths.sonarr.SPACE_ESTIMATES` with a 300 s expiry. Cache-warming hook.

Position in the manager tree: a submanager of the Sonarr service manager (`self.manager` / `self.sonarr_api` come from the injected `manager` kwarg). It loads the children listed below.

FETCH / CACHE / APPLY: this class itself only FETCHes (HTTP GET `rootfolder`, `disk_free_gb`) and CACHEs (`SPACE_ESTIMATES`). It performs no APPLY (no PUT/DELETE/POST).

Config keys read: `sonarr_instances` (via `config.get_sonarr_instances()`), default instance name (via the warm-cache path).
global_cache keys: reads/writes `CacheKeyPaths.sonarr.SPACE_ESTIMATES` (formatted per-instance).
dry_run: captured from `kwargs["dry_run"]` (default `False`) and forwarded to children via `init_kwargs`; this class has no destructive path of its own.
Singleton/concurrency: like all `BaseManager`s it is a process-wide singleton keyed on `(class, singleton_key)` and self-registers under the `"manager"` registry category.

## How it functions

Lifecycle: `__init__` calls `super().__init__` (BaseManager dependency injection + parent auto-link), then `self.register()`. It then resolves `sonarr_cache` (from the `cache_manager` kwarg, falling back to the parent's `sonarr_cache`) and `sonarr_api`, and assembles `init_kwargs` shared by all children.

Rather than the generic `ComponentManagerMixin.load_components`, it uses `split_components(...)` (from `scripts/support/utilities/managers/component_splitter.py`) to partition the classes into `critical` vs `noncritical` using `critical_keys = {"space","library","selection"}` (all are critical here). It then instantiates each in two loops, `setattr`-ing the instance on `self`, flipping a per-component registry flag `sonarr.storage.<name>_initialized`, and recording a per-component status in `self.load_summary`. Critical failures clear `all_critical_loaded`; the aggregate is stored as `self.all_components_loaded` and as registry flag `sonarr.storage_manager_initialized`. Finally `log_filtered_component_summary(...)` prints the one-line load summary.

Ordering note: children are instantiated in dict order — `library, selection, space` — so `library` exists on `self` before the children that look it up as their `selector` (e.g. `selection` grabs `self.manager.library`).

No decision is delegated to a `machine_learning` brain module from this class; it is pure plumbing + free-space reporting.

## Criteria & examples

- Free-space clamp: if the instance's `disk_free_gb` returns `inf` (e.g. no root folders or an unreadable mount), `get_free_space_per_instance` stores `0.0` for it. With the single `sonarr` instance the map is one entry: `{"sonarr": inf}` → stored as `{"sonarr": 0.0}`, so `get_minimum_free_space()` returns `0.0` and selection treats the misconfigured instance as full.
- Critical-load gate: if, say, the `selection` child raises during construction, `load_summary["selection"]` becomes `"❌ Failed: ..."`, `sonarr.storage.selection_initialized` is set `False`, and `sonarr.storage_manager_initialized` is set `False`, signalling the rest of the system that storage is degraded.

## In plain English

Think of this as the building superintendent for your TV library's hard drives. It doesn't personally do the heavy lifting — it hires three specialists (one who measures free space, one who keeps the catalogue of shows, and one who decides which drive a show belongs on) and makes sure they all share the same keys, the same logbook, and the same "are we just pretending?" (dry-run) instruction. On its own it can only answer one simple question: "how much free space is left?" — and since there's now a single `sonarr` drive, that minimum is just that one drive's gas gauge before a road trip with the Griswolds.

## Interactions

- **Parent:** the Sonarr service manager (supplies `sonarr_api`, `sonarr_cache`, `dry_run`, and `resolve_instance`).
- **Children (submanagers it loads):** `SonarrStorageLibraryManager`, `SonarrStorageSelectionManager`, `SonarrStorageSpaceManager`.
- **Services touched:** Sonarr HTTP API (`rootfolder`, plus `disk_free_gb`); `global_cache` for `SPACE_ESTIMATES`.
- **Brain modules:** none directly.
