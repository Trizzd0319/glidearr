# RadarrValidatorHealthManager

- **File** — `scripts/managers/services/radarr/validator/health.py`
- **One-liner** — Confirms each Radarr instance is reachable and reporting a sane API status by hitting its `system/status` endpoint.

## What it does (for a senior Python engineer)

`RadarrValidatorHealthManager(BaseManager, ComponentManagerMixin)` is a validation leaf under `RadarrValidatorManager` that performs a live FETCH against each Radarr instance and judges health by whether the response contains a `"version"` field. Unlike the keys submanager (which does a raw `requests.get`), this one goes through the shared `radarr_api._make_request` wrapper.

Position in the tree: `parent_name = "RadarrValidatorManager"`. No submanagers loaded. As a `BaseManager` singleton it has injected logger/config/global_cache/validator/registry, plus `self.radarr_api`, `self.instance_manager`, and `self.dry_run` resolved from kwargs/parent.

- **FETCH**: yes — `system/status` via `radarr_api._make_request`. **CACHE / APPLY**: none (results are returned/logged, not persisted).
- **Public methods**:
  - `verify_api_health(instance: str = None) -> bool` — health-checks one instance; if `instance` is omitted it picks the first discovered instance, or the literal `"default"` if none are discoverable.
  - `verify_all_instances_health() -> dict` — runs `_check_instance_health` for every discovered instance, returns `{instance: bool}`, and logs a "Health summary → Healthy: [...] | Unhealthy: [...]" line.
  - `run_selftest() -> dict` — thin wrapper that logs start, calls `verify_all_instances_health`, logs the result dict, and returns it.
- **Internal helpers**:
  - `_get_all_instances() -> list` — returns the list of instance names from `radarr_api.get_all_radarr_apis().keys()`, falling back to `instance_manager.get_all_radarr_apis().keys()`, else `[]`. Both paths are guarded by `hasattr` + try/except.
  - `_check_instance_health(instance) -> bool` — calls `radarr_api._make_request(instance, "system/status", fallback=None)`; returns `True` iff the response is truthy and contains `"version"` (logs "`<instance>` healthy (v...)"), else logs a warning/error and returns `False`.
- **External API endpoints**: `system/status` (Radarr API v3, via the shared client).
- **Config keys read**: none directly (instance discovery comes from `radarr_api`/`instance_manager`).
- **global_cache / Parquet keys**: none.
- **dry_run**: captured but irrelevant — health checks are read-only GETs, so they run identically in dry-run.
- **Singleton / concurrency**: `BaseManager` singleton; checks run sequentially per instance, no threading.

## How it functions

Lifecycle: `__init__` → `super().__init__` → `register()` → resolve deps → debug log. No `load_components`. At runtime a caller invokes `run_selftest()` (or `verify_all_instances_health` / `verify_api_health`). The control flow is: discover instances → for each, fire `system/status` → inspect for a `version` key → aggregate the boolean map. No `machine_learning` brain module is involved — health is a deterministic presence-of-`version` test.

## Criteria & examples

- An instance is **healthy** iff `_make_request(...)` returns a truthy object containing the key `"version"`. Anything else (None/empty/missing version/exception) is **unhealthy**.
- Example: `_make_request("1080", "system/status")` returns `{"version": "5.2.6", "appName": "Radarr"}` → logs "1080 healthy (v5.2.6)" → `True`.
- Example: the `4k` instance is down so `_make_request` raises a connection error → caught → logs "Health check failed for '4k': ..." → `False`.
- `verify_all_instances_health()` across those two → `{"1080": True, "4k": False}`, with the summary line `Healthy: ['1080'] | Unhealthy: ['4k']`.
- `verify_api_health()` with no argument and instances `["1080", "4k"]` checks `"1080"` (the first); with no instances at all it checks the literal `"default"`.

## In plain English

This is the inspector who actually phones each Radarr warehouse and asks "are you open, and what's your version?" If the warehouse answers with a real version number, it's marked open for business; if the line is dead or the answer is gibberish, it's marked closed. The "self-test" just calls every warehouse in turn and posts a tidy list of which ones picked up — like a roll call before the day's work starts, so nothing tries to ship orders to a warehouse that's offline.

## Interactions

- **Parent manager**: `RadarrValidatorManager`.
- **Sibling submanagers**: `RadarrValidatorAuthManager`, `RadarrValidatorCacheManager`, `RadarrValidatorKeysManager`.
- **Other services**: the shared `radarr_api` client (`_make_request`, `get_all_radarr_apis`) and `instance_manager` (fallback instance discovery).
- **Brain modules**: none.
