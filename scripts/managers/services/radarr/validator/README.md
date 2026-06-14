# RadarrValidatorManager

- **File** — `scripts/managers/services/radarr/validator/__init__.py`
- **One-liner** — The Radarr "validator" sub-tree orchestrator: it loads and wires up the four Radarr validation submanagers (auth, cache, health, keys) under one shared dependency set.

## What it does (for a senior Python engineer)

`RadarrValidatorManager(BaseManager, ComponentManagerMixin)` is a grouping/orchestrator manager. It does not perform any FETCH / CACHE / APPLY work itself; its sole responsibility is to construct its four child validation submanagers and expose them as attributes (`self.auth`, `self.cache`, `self.health`, `self.keys`).

Position in the manager tree: it sits under the Radarr service manager (its own `parent_name`/`__class__.__name__` is `RadarrValidatorManager`, and the four children declare `parent_name = "RadarrValidatorManager"`, so they auto-link to this instance as their parent through `BaseManager`). It is itself a `BaseManager`, so it is a process-wide singleton, injected with the shared logger/config/global_cache/validator/registry and auto-linked to its own parent (the Radarr manager that constructs it).

Key behavior in `__init__`:
- Resolves `self.radarr_api`, `self.instance_manager`, and `self.dry_run` from the explicit kwargs first, then falls back to the parent manager (`kwargs.get("manager")`). Note: `dry_run` is captured explicitly here (it is not inherited automatically by `BaseManager`).
- Builds `init_kwargs` (the shared dependency bundle, including `manager=self`) that is passed to each child constructor.
- Declares the component map `{"auth", "cache", "health", "keys"}` and marks all four as `critical_keys`.
- Calls `split_components(...)` (from `scripts/support/utilities/managers/component_splitter.py`) to partition the four classes into critical vs non-critical based on `parent_name_match`. Here all four are critical.
- Instantiates each component in a try/except, attaches it via `setattr(self, name, instance)`, and sets a per-component registry flag `radarr.validator.<name>_initialized` (True on success, False on failure). Failures are recorded in `self.load_summary[name]` as `"❌ Failed: <e>"` and, for critical components, flip `all_critical_loaded` to False.
- Sets `self.all_components_loaded = all_critical_loaded` and the registry flag `radarr.validator_manager_initialized`.
- Logs one filtered summary line via `log_filtered_component_summary(service_name="Radarr", ...)`.

Note: this manager hand-rolls the component loading loop rather than calling `ComponentManagerMixin.load_components`; it mixes the mixin in mainly for `log_filtered_component_summary`.

- **FETCH / CACHE / APPLY**: none directly — it is pure wiring.
- **External API endpoints**: none directly.
- **Config keys read**: none directly (children read config).
- **global_cache / Parquet keys**: none directly.
- **Registry flags written**: `radarr.validator.auth_initialized`, `radarr.validator.cache_initialized`, `radarr.validator.health_initialized`, `radarr.validator.keys_initialized`, and `radarr.validator_manager_initialized`.
- **dry_run**: captured and forwarded to children; this manager itself mutates nothing.
- **Singleton / concurrency**: `BaseManager` singleton; no threading of its own.

## How it functions

Lifecycle: `__init__` resolves deps → builds `init_kwargs` → `split_components` partitions the four classes → two sequential loops instantiate critical then non-critical components, setting registry flags and `load_summary` entries → final aggregate flag and summary log. There is no separate `run()` entry point; callers reach the validation behavior through the child attributes (e.g. `self.health.run_selftest()`, `self.keys.validate_all_keys()`).

No decision is delegated to a `machine_learning` brain module here — validation is mechanical reachability/cache-presence checking.

## Criteria & examples

- All four children (`auth`, `cache`, `health`, `keys`) are critical. If, say, `health` fails to construct, `radarr.validator.health_initialized` is set False, `load_summary["health"]` records the exception, and `all_critical_loaded`/`radarr.validator_manager_initialized` both become False.
- Example: with three children loading cleanly and `keys` raising during init, the summary would mark three OK and one failed, and `self.all_components_loaded` would be `False`.

## In plain English

Think of this as the shift supervisor for the Radarr "quality control" desk. The supervisor does not personally inspect anything; they just make sure the four inspectors are clocked in and at their stations: one who checks the keys to the building (auth), one who checks the filing cabinets are stocked (cache), one who phones the warehouse to confirm it's open (health), and one who tests the actual keys in the locks and keeps a copy of the paperwork (keys). If any inspector fails to show up, the supervisor flags it on the board so the rest of the system knows the desk isn't fully staffed.

## Interactions

- **Parent manager**: the Radarr service manager that constructs it (passes `manager`, `radarr_api`, `instance_manager`, `dry_run`).
- **Sibling/child submanagers it loads**: `RadarrValidatorAuthManager`, `RadarrValidatorCacheManager`, `RadarrValidatorHealthManager`, `RadarrValidatorKeysManager`.
- **Other services/utilities**: `split_components` (component partitioning), `LoggerManager` / `RegistryManager` (shared deps), `radarr_api` and `instance_manager` (forwarded to children).
- **Brain modules**: none.
