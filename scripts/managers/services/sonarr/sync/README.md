# SonarrSyncManager

- **File** — `scripts/managers/services/sonarr/sync/__init__.py`
- **One-liner** — The sync sub-tree orchestrator that loads and wires up the five Sonarr "config sync" submanagers (custom formats, root folders, media-management, naming, tags) under the Sonarr service.

## What it does (for a senior Python engineer)

`SonarrSyncManager(BaseManager, ComponentManagerMixin)` is a coordinator/aggregator. It owns no sync logic itself; it instantiates and holds the five child managers that push configuration across one or more Sonarr instances and exposes them as attributes (`self.custom_formats`, `self.folders`, `self.media_management`, `self.naming`, `self.tags`).

Position in the manager tree:
- **Parent** — `SonarrManager` (declared via the class attribute `parent_name = "SonarrManager"`, though `__init__` immediately overwrites `self.parent_name` with its own class name `"SonarrSyncManager"` so children resolve `SonarrSync` as their parent — see note below).
- **Submanagers loaded** — five classes, all in this directory:
  - `custom_formats` → `SonarrSyncCustomFormatsManager`
  - `folders` → `SonarrSyncFoldersManager`
  - `media_management` → `SonarrSyncMediaManager`
  - `naming` → `SonarrSyncNamingManager`
  - `tags` → `SonarrSyncTagsManager`

FETCH / CACHE / APPLY: none directly. It is pure composition; the verbs live in the children.

External API endpoints touched: none directly.

Config keys read: none directly (it forwards `config` to children).

global_cache / Parquet keys: none directly. It builds a `CacheKeyBuilder()` (`self.key_builder`) and passes it into children's `init_args`.

dry_run behavior: it reads `kwargs.get("dry_run", False)` and threads it into every child via `init_args["dry_run"]`. It performs no mutations itself.

Singleton / concurrency: as a `BaseManager` it is a process-wide singleton keyed by `(class, singleton_key)`. Construction is sequential (a plain `for` loop over components); no threads.

Public surface: there are no public "run" methods beyond `__init__`. Callers reach the actual sync operations through the child attributes (e.g. `sync_manager.naming.sync_naming_settings(...)`).

## How it functions

Lifecycle:
1. `__init__` calls `super().__init__(...)` (BaseManager wiring: logger/config/global_cache/validator/registry, auto-link to parent) then `self.register()`.
2. Builds `self.key_builder = CacheKeyBuilder()`, and empty `self.sonarr_apis = {}` and `self.load_summary = {}`.
3. Assembles `init_args` — the shared dependency bundle handed to every child: `logger`, `config`, `cache` (the global_cache), `validator`, `registry`, `manager=self`, `sonarr_api` (the injected `sonarr_api`), `instance_manager` (pulled off `sonarr_api.instance_manager`), `key_builder`, and `dry_run`.
4. Defines `all_component_classes` (the five classes above) and marks **all five as critical** (`critical_keys`).
5. Calls `split_components(...)` from `scripts/support/utilities/managers/component_splitter.py` to partition the map into `critical_components` / `noncritical_components` (here all five are critical).
6. Iterates critical then noncritical components: for each, instantiates `cls(**init_args)`, `setattr(self, name, instance)`, sets a registry flag `sonarr.sync.<name>_initialized` (True on success, False on exception), and records a status string in `self.load_summary`. A failure of any critical component flips `all_critical_loaded = False` but does not abort the loop.
7. Sets `self.all_components_loaded` and the registry flag `sonarr.sync_manager_initialized` to `all_critical_loaded`.
8. Calls `self.log_filtered_component_summary(...)` (from `ComponentManagerMixin`) to emit one summary line.

Note: this manager does **not** call `ComponentManagerMixin.load_components()`; it open-codes its own load loop (using `split_components` + manual `setattr` + per-name registry flags) instead of the shared helper. The behavior is equivalent (attach children as attributes, inject shared deps, set per-component init flags) but the summary uses `log_filtered_component_summary` rather than the mixin's single `[Name] N/N:` line.

Brain delegation: none. No `machine_learning` module is touched here.

## Criteria & examples

- All five components are "critical." If, say, `tags` fails to construct, `self.load_summary["tags"]` becomes `"❌ Failed: <error>"`, the registry flag `sonarr.sync.tags_initialized` is set False, `all_critical_loaded` becomes False, and `sonarr.sync_manager_initialized` is set False — but the other four still load.
- Concrete: with `dry_run=True` passed in, every child receives `dry_run=True`, so a downstream call like `naming.sync_naming_settings(cfg)` will log `[DRY-RUN] Would apply naming config to <instance>` instead of issuing the PUT.

## In plain English

Think of this as the manager of a small department whose job is "make sure every TV-download server is set up the same way." This manager itself doesn't change any settings — it just hires and equips five specialists (one for naming rules, one for folders, one for tags, one for quality "custom formats," and one for general media settings) and hands each the same toolbox (logger, config, the connection to Sonarr). If one specialist can't show up to work, the others still do their jobs, and the manager keeps a little attendance sheet noting who made it in.

## Interactions

- **Parent** — `SonarrManager` (the Sonarr service manager), which injects `sonarr_api`.
- **Submanagers** — `SonarrSyncCustomFormatsManager`, `SonarrSyncFoldersManager`, `SonarrSyncMediaManager`, `SonarrSyncNamingManager`, `SonarrSyncTagsManager`.
- **Shared infra** — `BaseManager`, `ComponentManagerMixin`, `RegistryManager` (flags), `CacheKeyBuilder`, `split_components`.
- **Brain modules** — none.
