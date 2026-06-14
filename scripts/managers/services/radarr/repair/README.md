# RadarrRepairWrapperManager

- **File** — `scripts/managers/services/radarr/repair/__init__.py`
- **One-liner** — The package facade that loads all eight Radarr-repair sub-managers and runs their scan/repair passes across every configured Radarr instance.

## What it does (for a senior Python engineer)

`RadarrRepairWrapperManager` is the entry point of the Radarr "repair" subsystem. It is a `BaseManager` + `ComponentManagerMixin` that owns the lifecycle of eight component managers and exposes a single `run()` that fans out over Radarr instances.

Key behaviors:

- **Where it sits.** Its `parent_name` is set to its own class name (`RadarrRepairWrapperManager`). It is constructed by a Radarr-side parent (passed via `kwargs["manager"]`) from which it inherits `radarr_api`, `instance_manager`, and `dry_run` (falling back through the parent attributes if not passed explicitly).
- **Submanagers loaded.** Rather than the mixin's generic `load_components`, it builds an explicit `init_kwargs` dict and uses `split_components(...)` (from `support/utilities/managers/component_splitter.py`) to partition this map into critical vs non-critical:
  - `anomaly` → `RadarrRepairAnomalyManager`
  - `interface` → `RadarrRepairInterfaceManager`
  - `manager` → `RadarrRepairManager`
  - `metadata` → `RadarrRepairMetadataManager`
  - `orphans` → `RadarrRepairOrphansManager`
  - `quality` → `RadarrRepairQualityManager`
  - `storage` → `RadarrRepairStorageManager`
  - `tags` → `RadarrRepairTagsManager`
  - `critical_keys` lists ALL eight, so in practice every component is treated as critical.
- It instantiates each class with the shared `init_kwargs` (logger, config, global_cache, validator, registry, radarr_api, instance_manager, `manager=self`, dry_run), attaches the instance as `self.<name>`, and sets a registry flag `radarr.repair.<name>_initialized` (True on success, False on the exception path). Per-component status is recorded in `self.load_summary` as a checkmark/cross string.
- After loading it sets `self.all_components_loaded` and the registry flag `radarr.repair_manager_initialized` to whether all critical components loaded, then logs one filtered summary line via `log_filtered_component_summary`.
- **FETCH / CACHE / APPLY.** None directly — it is pure orchestration. It does not touch the Radarr HTTP API itself; the FETCH/CACHE/APPLY work lives in the eight submanagers.
- **Config keys.** None read directly (config is inherited and threaded down).
- **global_cache / Parquet keys.** None read or written directly.
- **dry_run.** Resolved at init and threaded into every component's `init_kwargs`; the wrapper itself performs no mutations.
- **Singleton / concurrency.** As a `BaseManager` it is a process-wide singleton keyed by class + singleton key. `run()` is sequential — no threads spawned here.

Public methods:

- `run(instance: str | None = None) -> dict` — Runs the repair pipeline. If `instance` is given it runs only that one; otherwise it calls `_all_instances()` to enumerate all Radarr instances. For each instance it invokes `.run(inst)` on the components in fixed order: `anomaly`, `metadata`, `quality`, `tags`, `orphans`, `storage` (note: `interface` and `manager` are NOT run — they are helper/adapter components, not scan passes). Each component's result is stored under `all_results[inst][name]`; a component exception is caught and stored as `{"error": str(e)}`. Returns `{instance: {component: result}}`.
- `_all_instances()` (internal) — Returns the list of Radarr instance keys via `instance_manager.get_all_radarr_apis().keys()` (falling back to `radarr_api.get_all_radarr_apis()`), or `[]` if neither is available.

## How it functions

Lifecycle: `__init__` → `register()` → resolve `radarr_api`/`instance_manager`/`dry_run` from kwargs-or-parent → build `init_kwargs` → `split_components` → instantiate each critical then non-critical component, attaching it and flagging the registry → set aggregate flags → log the component summary.

At run time, `run()` is a double loop: outer over instances, inner over the six scan/repair components in a hardcoded order. It is defensive: every component is checked for existence and a `run` attribute before calling, and each call is wrapped so one failing component cannot abort the rest.

No decision logic lives here; value judgements are delegated downstream (the `anomaly` component is the one that calls into `machine_learning`).

## Criteria & examples

The only "rule" at this layer is the run order and the critical/non-critical split. Example: with two instances `radarr-1080` and `radarr-4k` configured and `instance=None`, `run()` produces `{"radarr-1080": {"anomaly": {...}, "metadata": {...}, ...}, "radarr-4k": {...}}`. If `metadata.run()` raises for `radarr-4k`, the result becomes `{"radarr-4k": {..., "metadata": {"error": "..."}, "quality": {...}, ...}}` — the remaining components still run.

## In plain English

Think of this as the manager of a movie-library cleanup crew. The crew has eight specialists (one checks for weird states, one fixes metadata, one watches disk space, one tidies tags, and so on). When you say "do a cleanup," this manager sends the crew through every movie shelf (every Radarr "instance," e.g. your HD shelf and your 4K shelf) and lets each specialist do their part. If one specialist trips up on a shelf, the manager just notes it and keeps the others working — your whole cleanup never stops because of one hiccup.

## Interactions

- **Parent manager** — A Radarr-side manager passed as `kwargs["manager"]` (the source of `radarr_api`, `instance_manager`, `dry_run`).
- **Sibling/child submanagers** — The eight `RadarrRepair*Manager` classes it loads (anomaly, interface, manager, metadata, orphans, quality, storage, tags).
- **Brain modules** — None directly; only its `anomaly` child delegates into `machine_learning`.
- **Other services** — `instance_manager` / `radarr_api` for instance enumeration; `registry` for init flags.
