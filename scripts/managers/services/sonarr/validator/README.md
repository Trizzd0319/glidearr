# SonarrValidatorManager

**File** — `scripts/managers/services/sonarr/validator/__init__.py`
**One-liner** — The orchestrator that bootstraps the Sonarr "validator" subtree (auth, cache, health, key, and raw-API helpers) and runs a lightweight pre-flight audit of every Sonarr instance's credentials and reachability.

## What it does (for a senior Python engineer)

`SonarrValidatorManager(BaseManager, ComponentManagerMixin)` is the top of the Sonarr *validator* package. Its declared `parent_name = "SonarrManager"`, so the BaseManager auto-link machinery wires it under the `SonarrManager` / `SonarrInstanceManager` tree and it inherits that parent's logger/config/cache/validator.

Responsibilities:
- Establish a "dual cache" view: it resolves both `self.sonarr_cache` (from the `sonarr_cache` kwarg or the parent's attribute) and `self.global_cache` (the global cache).
- Instantiate the validator submanagers and attach each as an attribute on `self`.
- Track a `self.load_summary` dict (per-component `"✅ Loaded"` / `"❌ Failed: <err>"`) and an `all_components_loaded` boolean.
- Expose `audit_bootstrap_instances(...)` as the single public entry point used by `SonarrInstanceManager` during its own init.

Component loading is NOT done through `ComponentManagerMixin.load_components` here; it is hand-rolled. The component map is:

| attribute name      | class                          | critical? |
|---------------------|--------------------------------|-----------|
| `auth_handler`      | `SonarrValidatorAuthManager`   | yes       |
| `cache_manager`     | `SonarrCacheManager` (imported from `scripts.managers.services.sonarr.cache`, NOT the local `cache.py`) | yes |
| `health_validator`  | `SonarrValidatorHealthManager` | yes       |
| `key_validator`     | `SonarrValidatorKeysManager`   | yes       |
| `api_factory`       | `SonarrValidatorFactoryManager`| no        |

Note: `cache_manager` resolves to the package's sibling `SonarrCacheManager` (`scripts/managers/services/sonarr/cache.py`), not the local `SonarrValidatorCacheManager` defined in this same folder's `cache.py`. The local cache validator is documented in `cache.md` but is not wired in here.

The map is split into critical vs non-critical via `split_components(...)` (from `scripts.support.utilities.managers.component_splitter`), passing `critical_keys = {"auth_handler", "cache_manager", "health_validator", "key_validator"}`, the parent-name match, and the shared `init_args`. Each component is constructed in a `try/except`; on success it sets registry flag `sonarr.validator.<name>_initialized = True` and records `"✅ Loaded"`; on failure it sets the flag `False`, records the error, and (for critical components only) clears `all_critical_loaded`. After the loop it sets `self.all_components_loaded` and registry flag `sonarr.validator_manager_initialized` to that aggregate, and emits one filtered summary line via `log_filtered_component_summary(service_name="Sonarr", ...)`.

`init_args` injected into every child: `logger`, `config`, `global_cache`, `validator`, `registry`, `sonarr_cache`, `manager=self`, and `sonarr_api=sonarr_api or self` (so the validator subtree's "API" defaults to this manager when no explicit `sonarr_api` is passed).

FETCH / CACHE / APPLY: this class itself is an orchestrator — it neither GETs nor writes cache directly. It delegates FETCH-style reachability/health probes to `health_validator` and `key_validator`.

Public methods:
- `audit_bootstrap_instances(validate_keys=True, validate_health=True)` — Lightweight bootstrap validation. When `validate_keys` is true it calls `self.key_validator.run_credentials_only()` and stores the result under `summary["credentials"]`. When `validate_health` is true it calls `self.health_validator.verify_all_instances_health()` under `summary["health"]`. Logs `🩺 Bootstrap Audit Results: {summary}` and returns the `summary` dict. (Note: it calls `verify_all_instances_health`, the public-named method; the health manager's body defines `_verify_all_instances_health` and a public `run` — see Interactions.)

Config keys read: none directly in this class (children read `sonarr_instances`).
global_cache / Parquet keys: none written here.
Registry flags written: `sonarr.validator.<name>_initialized` (per component) and `sonarr.validator_manager_initialized`.

Singleton / concurrency: as a `BaseManager`, instances are cached process-wide keyed by `(class, singleton_key)`. No threading inside this class.

## How it functions

Lifecycle: `__init__` → `super().__init__` (injects shared deps + auto-link to `SonarrManager`) → resolve dual cache → `self.register()` → build `all_component_classes` map → `split_components` → construct critical then non-critical components in two loops, stamping registry flags and `load_summary` → set aggregate flags → `log_filtered_component_summary`. There is no separate `run()`; the manager's "work" is the on-demand `audit_bootstrap_instances`, intended to be invoked early by `SonarrInstanceManager.__init__` to catch credential/reachability problems before any real Sonarr work begins.

No machine_learning brain module is consulted; this is pure pre-flight plumbing.

## Criteria & examples

- A component is *critical* iff its key is in `{"auth_handler", "cache_manager", "health_validator", "key_validator"}`. If `health_validator` raises during construction, `all_critical_loaded` flips to `False`, `sonarr.validator_manager_initialized` is set `False`, and `load_summary["health_validator"]` becomes e.g. `"❌ Failed: connection refused"`. The non-critical `api_factory` failing would be recorded but would NOT flip the aggregate.
- Worked example of `audit_bootstrap_instances()` with both flags default-true and two instances "1080" (key present, reachable) and "4k" (key missing): `summary["credentials"]` → `{"valid": 1, "missing": 1, "errored": [], "success": False}`; `summary["health"]` → `{"1080": True, "4k": False}` (4k cannot ping `system/status` with no key). The combined dict is logged and returned so the caller can decide whether to abort.

## In plain English

Think of this as the gate agent at an airport who, before letting any plane (a Sonarr server) onto the runway, checks two things: does the pilot have a valid boarding pass (the API key), and does the plane actually radio back when you call it (the health ping). It doesn't fly anything itself — it just lines up the specialist inspectors, runs a quick pre-flight checklist, and hands back a clipboard saying which servers are good to go and which are missing credentials or unreachable. If a *critical* inspector can't even show up for work, it raises a red flag so the rest of the system knows the Sonarr setup isn't trustworthy yet.

## Interactions

- **Parent manager:** `SonarrManager` (declared via `parent_name`); in practice invoked from `SonarrInstanceManager.__init__`.
- **Submanagers it constructs:** `SonarrValidatorAuthManager` (`auth_handler`), `SonarrCacheManager` (`cache_manager`, the sibling service cache), `SonarrValidatorHealthManager` (`health_validator`), `SonarrValidatorKeysManager` (`key_validator`), `SonarrValidatorFactoryManager` (`api_factory`).
- **Helpers:** `split_components` for critical/non-critical partitioning; `log_filtered_component_summary` for the one-line load summary.
- **Brain modules:** none — no `machine_learning` delegation.
