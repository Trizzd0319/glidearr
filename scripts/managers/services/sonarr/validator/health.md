# SonarrValidatorHealthManager

**File** — `scripts/managers/services/sonarr/validator/health.py`
**One-liner** — Pings every configured Sonarr instance's `system/status` endpoint and reports which instances are responsive (return a `version`) and which are not.

## What it does (for a senior Python engineer)

`SonarrValidatorHealthManager(BaseManager, ComponentManagerMixin)` is a reachability/liveness probe for Sonarr instances. It sets `self.parent_name = self.__class__.__name__` for logging context.

State resolved at init:
- Dual cache: `self.sonarr_cache` and `self.global_cache` (resolved BEFORE `super().__init__`, and `global_cache` is passed into `super().__init__`).
- `self.manager` — the `manager` kwarg, or fallback `self.registry.get("manager", self.parent_name)`.
- `self.sonarr_apis` — from the `sonarr_apis` kwarg or the manager's `sonarr_apis` attribute (`{instance_name: api_client}`).
- `self.dry_run` — from kwarg/manager, default `False`.
- Raises `ValueError` if no logger.

Public methods:
- `run()` — calls `self._verify_all_instances_health()`, logs `📝 Health Check Summary: <results>`, and returns the `results` dict (`{instance_name: bool}`).

FETCH / CACHE / APPLY:
- FETCH only: per instance it calls `api._make_request(name, "system/status")` (an HTTP GET to Sonarr's status endpoint via the instance's API client).
- No CACHE writes, no APPLY. dry_run is read and stored but does not change behavior — health checks are read-only GETs and run regardless.

External API endpoint touched: `system/status` (Sonarr `/api/v3/system/status`, via the API client).
Config keys: none directly.
global_cache / Parquet: none read or written.

Singleton / concurrency: standard `BaseManager` caching; instances iterated sequentially, no threading.

Naming note: the public method is `run()` and the worker is `_verify_all_instances_health()`. The parent `SonarrValidatorManager.audit_bootstrap_instances` calls `self.health_validator.verify_all_instances_health()` (no leading underscore) — that public-named alias is not defined in this file, so the call resolves only if it exists on a base/mixin; the in-file implementation is the underscore-prefixed `_verify_all_instances_health`.

## How it functions

Lifecycle: `__init__` (resolve dual cache → `super().__init__` → register → resolve manager/sonarr_apis/dry_run) → caller invokes `run()`.

Control flow:
1. `run()` → `_verify_all_instances_health()`.
2. `_verify_all_instances_health()` loops over `self.sonarr_apis.items()`, calling `_check_instance_health(name, api)` for each and collecting `{name: bool}`. It then partitions into `healthy`/`unhealthy` lists and logs `🩺 Sonarr API status → ✅ Healthy: [...] | ❌ Unhealthy: [...]`, and returns the per-instance dict.
3. `_check_instance_health(name, api)` calls `api._make_request(name, "system/status")`. It returns `True` iff the result is truthy and contains a `"version"` key (logging `✅ <name> responsive (v<version>)`). A truthy-but-malformed response logs a warning and returns `False`. Any exception logs an error and returns `False`.

No machine_learning delegation — this is a pure connectivity check.

## Criteria & examples

- Healthy ⇔ `_make_request(name, "system/status")` returns a dict containing `"version"`.
- Example: instance `1080` responds with `{"version": "4.0.1.929", "appName": "Sonarr", ...}` → `_check_instance_health` logs `✅ 1080 responsive (v4.0.1.929)` and returns `True`.
- Example: instance `4k` is down and `_make_request` raises `ConnectionError` → logs `❌ Health check failed for '4k': ...` and returns `False`.
- Example: instance `720` returns `{}` (truthy-empty would be falsy here; an actual `{"status": "ok"}` without `version`) → logs `⚠️ 720 returned unexpected system/status response.` and returns `False`.
- Aggregate for those three: `{"1080": True, "4k": False, "720": False}`, with `Healthy: ['1080'] | Unhealthy: ['4k', '720']`.

## In plain English

This is the "are you there?" knock on each TV server's door. It calls each server and listens for a specific reply — the server announcing its version number, like saying "Yep, I'm Sonarr version 4, I'm awake." If the server answers properly, it's marked healthy; if it stays silent, gives a weird answer, or the line is dead, it's marked unhealthy. At the end you get a simple roster of which servers are up and which are down — handy for catching a server that crashed or got unplugged before the system tries to use it.

## Interactions

- **Parent manager:** loaded as `health_validator` by `SonarrValidatorManager`, which calls into it during `audit_bootstrap_instances`.
- **Siblings:** `SonarrValidatorKeysManager` (credential side of the same audit) and `SonarrValidatorCacheManager`.
- **External:** per-instance Sonarr API client `_make_request(..., "system/status")`.
- **Brain modules:** none.
