# RadarrSyncManager

**File** — `scripts/managers/services/radarr/sync/__init__.py`
**One-liner** — The orchestrator submanager that loads and owns the five Radarr "sync" leaf managers (custom formats, root folders, media management, naming, tags) so that configuration can be kept consistent across multiple Radarr instances.

## What it does (for a senior Python engineer)

`RadarrSyncManager(BaseManager, ComponentManagerMixin)` is a pure container/orchestrator. It performs no FETCH / CACHE / APPLY of its own — all real I/O lives in the five leaf managers it loads.

Key behavior:

- **Position in the manager tree.** Its parent is `RadarrManager` (`scripts/managers/services/radarr/__init__.py`), which loads it under the component key `sync` with the extra dependency `instance_manager`. So the chain is `Main → RadarrManager → RadarrSyncManager → {custom_formats, folders, media_management, naming, tags}`.
- **`parent_name`.** Both a class attribute and re-set in `__init__` to `"RadarrSyncManager"`. This is the value the leaf managers must match for the component splitter to accept them.
- **Dependency wiring.** From `kwargs["manager"]` (its parent) or its own kwargs it captures `radarr_api` (the `RadarrInstanceManager`, the service-specific Radarr API client), `instance_manager`, and `dry_run` (defaulting to the parent's `dry_run`, else `False`).
- **Component loading (custom, not the stock `load_components`).** It builds an `init_kwargs` dict with the shared deps plus `manager=self`, then runs `split_components(...)` (`scripts/support/utilities/managers/component_splitter.py`) to separate the component map into `critical` and `noncritical` groups. Every one of the five keys is in `critical_keys`, so in practice all five are critical. For each component it instantiates the class with `init_kwargs`, attaches it as `self.<name>`, sets registry flag `radarr.sync.<name>_initialized` to `True`/`False`, and records a status string in `self.load_summary`.
- **Registry flags written.** Per component: `radarr.sync.custom_formats_initialized`, `radarr.sync.folders_initialized`, `radarr.sync.media_management_initialized`, `radarr.sync.naming_initialized`, `radarr.sync.tags_initialized`. Aggregate: `radarr.sync_manager_initialized` (`True` only if all critical components loaded).
- **Summary line.** Calls `self.log_filtered_component_summary(service_name="Radarr", component_label="RadarrSyncManager", ...)`, which silently appends a row to the logger's end-of-run component table rather than printing inline.
- **No public run method.** There is no `run()` / `sync_all()` entry method here; callers reach in and invoke methods on the leaf managers directly (e.g. `radarr.sync.tags.sync_tags_across_instances()`).
- **Config keys read:** none directly (the leaves read their own).
- **global_cache / Parquet:** none directly.
- **dry_run:** captured and forwarded into `init_kwargs` so each leaf inherits it; this manager mutates nothing itself.
- **Singleton / concurrency:** as a `BaseManager` it is a process-wide singleton keyed by class + singleton key; loading is single-threaded and synchronous.

## How it functions

Lifecycle: `__init__` → `super().__init__` (injects logger/config/global_cache/validator/registry, auto-links to parent `RadarrManager`) → `self.register()` → capture deps → `split_components` → loop-instantiate critical then noncritical components, setting attributes + registry flags → set aggregate flag → `log_filtered_component_summary`.

Notable internal detail: `split_components` introspects each *noncritical* candidate by constructing a throwaway instance and checking its `parent_name` equals `"RadarrSyncManager"`. Because all five keys are listed as critical here, this introspection path is effectively unused for this manager — all five are loaded directly in the critical loop.

This manager delegates no decisions to a `machine_learning` brain module; it is plumbing only.

## Criteria & examples

- **Critical vs. all-loaded gate.** `all_critical_loaded` starts `True` and is set `False` if any of the five critical components throws during construction. Example: if `RadarrSyncTagsManager.__init__` raised, then `radarr.sync.tags_initialized=False`, `load_summary["tags"]="❌ Failed: ..."`, and `radarr.sync_manager_initialized=False`, while the other four would still be `True`.
- **Noncritical failures are tolerated.** Failures in the noncritical loop set the per-component flag `False` but never flip `all_critical_loaded`. (In the current config there are no noncritical components, so this branch is dormant.)

## In plain English

Think of a movie theater chain with five identical screening rooms (Radarr "instances"). This manager is the duty manager who, at the start of the shift, makes sure five specialist staff are all clocked in and ready: the one who labels reels ("tags"), the one who knows which storage closets exist ("folders"), the one who sets projector quality ("media management"), the one who names the film cans consistently ("naming"), and the one who manages the special-edition tagging rules ("custom formats"). The duty manager doesn't run the projectors personally — they just confirm every specialist showed up and noted who, if anyone, called in sick.

## Interactions

- **Parent:** `RadarrManager`.
- **Sibling submanagers (loaded here):** `RadarrSyncCustomFormatsManager`, `RadarrSyncFoldersManager`, `RadarrSyncMediaManager`, `RadarrSyncNamingManager`, `RadarrSyncTagsManager`.
- **Shared services:** the `radarr_api` (`RadarrInstanceManager`) and `instance_manager` it forwards to every leaf; the `RegistryManager` for flags; the logger's component-summary table.
- **Brain modules:** none.
