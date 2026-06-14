# TautulliValidatorManager

- **File** — `scripts/managers/services/tautulli/validator.py`
- **One-liner** — A package-level no-op validator stub that exists only to satisfy `TautulliManager`'s component registry import; the real instance-level validation lives in `tautulli/instances/validator.py`.

## What it does (for a senior Python engineer)

`TautulliValidatorManager(BaseManager)` is a deliberate stub. Its sole reason to exist is that `TautulliManager.__init__` imports it and registers it as the `validator_manager` component, so the package must export the symbol or imports of any `tautulli.*` submodule would fail.

- **Manager-tree position.** `parent_name = "TautulliManager"`, so when instantiated it auto-links to `TautulliManager` and inherits the shared logger/config/cache/validator. It is registered (non-critically) as the `validator_manager` component but is loaded lazily — `TautulliManager.prepare()` only eagerly loads the seven `critical_keys`, and this is not one of them.
- **FETCH / CACHE / APPLY.** None. It touches no HTTP endpoints, no config keys, and no global_cache / Parquet keys.
- **dry_run / concurrency.** Inherits BaseManager's singleton behaviour; otherwise irrelevant — it does no work.

**Public methods.**
- `__init__(logger, config, global_cache, validator, registry, **kwargs)` — pass-through to `BaseManager.__init__`.
- `validate() -> bool` — unconditionally returns `True`.

## How it functions

There is no lifecycle to speak of beyond BaseManager construction. `validate()` always succeeds. The real per-instance validation (URL/API-key reachability checks for a configured Tautulli instance) lives in `TautulliInstanceValidatorManager` (`tautulli/instances/validator.py`), which is a separate work item. No decision is delegated to any `machine_learning/` brain module.

Note: the module docstring states `TautulliManager` is commented out of the startup path in `main.py`. That comment is stale — `scripts/main.py` (lines 80–89) actively constructs and runs `TautulliManager`. The stub itself is unaffected either way.

## Criteria & examples

No thresholds, guards, or selection rules. `validate()` returns `True` regardless of input — e.g. with a completely empty `tautulli` config block it still returns `True`, because real connectivity validation is the instance validator's job, not this stub's.

## In plain English

This is a placeholder doorman who waves everyone through. The app's wiring expects a "validator" component to exist at this spot in the Tautulli package, so a stand-in is provided that always says "looks fine to me." The doorman who actually checks IDs (does Tautulli answer, is the key right) works one floor down, in the instances folder.

## Interactions

- **Parent manager:** `TautulliManager` (registers it as the `validator_manager` component).
- **Sibling/real counterpart:** `TautulliInstanceValidatorManager` in `tautulli/instances/validator.py` — the actual validation logic (documented separately).
- **Brain modules / other services:** none.
