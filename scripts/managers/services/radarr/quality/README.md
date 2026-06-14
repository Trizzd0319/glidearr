# RadarrQualityManager

**File** — `scripts/managers/services/radarr/quality/__init__.py`
**One-liner** — The container manager for everything quality-related in Radarr: it constructs and holds the six quality submanagers (adjustments, custom formats, file sizes, profile selector, space pressure, universe) as attributes on itself.

## What it does (for a senior Python engineer)

`RadarrQualityManager(BaseManager, ComponentManagerMixin)` is a pure orchestration/container node. It performs no FETCH / CACHE / APPLY of its own — it exists to instantiate and own the quality submanager tree and to publish their init status into the registry.

Responsibilities:
- Resolve shared dependencies once and propagate them to all children: `radarr_api`, `instance_manager`, and `dry_run` are read from the explicit kwargs first, then fall back to the parent manager (`kwargs["manager"]`).
- Build a single `init_kwargs` dict (logger, config, global_cache, validator, registry, radarr_api, instance_manager, `manager=self`, dry_run) that is splatted into every child constructor — so the whole subtree shares one logger/config/cache/validator and the same `radarr_api` and `dry_run`.
- Instantiate the six component classes and attach each as an attribute named by its key.

It does NOT use `ComponentManagerMixin.load_components` directly. Instead it calls `split_components(...)` (from `scripts/support/utilities/managers/component_splitter.py`) to partition the component map into `critical_components` and `noncritical_components`, then instantiates each itself in two loops. Here all six keys are listed as critical, so the noncritical loop is empty in practice.

Component map (attribute name → class):
- `adjustments` → `RadarrQualityAdjustmentManager`
- `custom_formats` → `RadarrCustomFormatsManager`
- `file_sizes` → `RadarrFileSizesManager`
- `selector` → `RadarrQualitySelectorManager`
- `space_pressure` → `RadarrSpacePressureManager`
- `universe` → `RadarrQualityUniverseManager`

Per-component it sets a registry flag `radarr.quality.<name>_initialized` (True on success, False on `Exception`) and records a human-readable status in `self.load_summary[name]` ("✅ Loaded" / "❌ Failed: <e>"). A failure in any critical component flips `all_critical_loaded` to False; noncritical failures do not. Finally it sets `self.all_components_loaded` and the registry flag `radarr.quality_manager_initialized`, then emits a single filtered summary line via `log_filtered_component_summary`.

- **Parent manager**: `RadarrManager` (this manager's `parent_name` is its own class name; each child sets `parent_name = "RadarrQualityManager"`, so the children auto-link up to this manager).
- **FETCH / CACHE / APPLY**: none directly; delegated to children.
- **External API endpoints**: none directly.
- **config keys read**: none directly.
- **global_cache / Parquet keys**: none directly.
- **dry_run**: not enforced here; resolved and forwarded to every child.
- **Singleton / concurrency**: like all `BaseManager` instances it is a process-wide singleton keyed by class + singleton_key; child construction is sequential, no threading.

## How it functions

Lifecycle:
1. `__init__` sets `parent_name`, calls `super().__init__(...)` (BaseManager wiring + parent auto-link), then `self.register()`.
2. Resolves `radarr_api` / `instance_manager` / `dry_run` from kwargs or parent.
3. Builds `init_kwargs`, declares `all_component_classes` and `critical_keys` (all six), and calls `split_components`.
4. Instantiates critical components, then noncritical components, setting registry flags and `load_summary` for each.
5. Sets aggregate flags and logs the summary.

There is no `run()` method here — running the actual quality work is the job of the individual submanagers (e.g. `space_pressure.run(instance)`, `universe.run(instance, free_space_gb)`), invoked by the parent Radarr flow.

No decision is delegated to a `machine_learning` brain module at this level; the children do that.

## Criteria & examples

The only rule here is the critical/noncritical split. All six keys are critical, so if (for example) `RadarrSpacePressureManager` raises during construction (e.g. it cannot resolve `dry_run` and raises `ValueError`), then:
- `self.load_summary["space_pressure"] = "❌ Failed: <error>"`
- registry flag `radarr.quality.space_pressure_initialized = False`
- `all_critical_loaded = False`, so `radarr.quality_manager_initialized = False`.

If instead an optional/noncritical component failed, the manager would still report `radarr.quality_manager_initialized = True`.

## In plain English

Think of this as the manager of a "quality department" at a film archive. The department head doesn't personally re-encode films or delete anything — they just hire and supervise six specialists: one who adjusts technical size rules, one who manages tagging/scoring formats, one who estimates how big a file should be, one who picks the right quality level for each movie, one who frees up shelf space when storage is full, and one who looks after the Marvel/DC-style franchise box sets. The head makes sure all six share the same office tools and the same "are we just rehearsing or doing it for real?" (dry-run) instruction, and reports to the front office which specialists showed up for work.

## Interactions

- **Parent**: `RadarrManager`.
- **Submanagers (siblings of each other)**: `RadarrQualityAdjustmentManager`, `RadarrCustomFormatsManager`, `RadarrFileSizesManager`, `RadarrQualitySelectorManager`, `RadarrSpacePressureManager`, `RadarrQualityUniverseManager`.
- **Helpers**: `split_components` (component partitioning), `log_filtered_component_summary` (from `ComponentManagerMixin`).
- **Brain modules**: none directly.
