# SonarrValidatorFactoryManager

**File** — `scripts/managers/services/sonarr/validator/factory.py`
**One-liner** — A thin raw-HTTP helper that performs direct Sonarr GET requests (with `requests`) for ad-hoc endpoints, used by validator submodules that need to hit `/api/v3/...` paths outside the formal API-client models.

## What it does (for a senior Python engineer)

`SonarrValidatorFactoryManager(BaseManager, ComponentManagerMixin)` wraps the `requests` library to do bare GETs. It sets `self.parent_name = self.__class__.__name__`.

State at init:
- `self.manager` — `manager` kwarg or `self.registry.get("manager", self.parent_name)`.
- Dual cache: `self.sonarr_cache`, `self.global_cache`.
- `self.dry_run` — from kwarg/manager, default `False` (stored but unused; these are read-only GETs).
- `self.default_headers = {"Content-Type": "application/json"}`.

Public methods:
- `get_raw(url, headers=None, timeout=5)` — Performs `requests.get(url, headers=merged_headers, timeout=timeout)`, calls `response.raise_for_status()`, and returns `response.json()`. On any exception it logs `⚠️ Failed raw GET to <url>: <err>` and returns `{}`. `final_headers` merges `self.default_headers` with the caller's `headers`.
- `get_instance_raw(instance_obj, endpoint)` — Given a raw Sonarr instance dict, derives the base URL from `instance_obj["base_url"]` or `["url"]`, the token from `["api"]` / `["api_key"]` / `["token"]`, sets header `{"X-Api-Key": token}`, builds `f"{base.rstrip('/')}/api/v3/{endpoint.lstrip('/')}"`, and delegates to `get_raw`. On exception logs `⚠️ Failed API call to <endpoint>: ...` and returns `{}`.

FETCH / CACHE / APPLY:
- FETCH only — direct GETs over `requests`. No CACHE writes, no APPLY/mutation.

External API endpoints touched: arbitrary Sonarr `/api/v3/<endpoint>` paths (the package docstring/usage points at things like `system/status`); whatever URL the caller supplies to `get_raw`.
Config keys read: none.
global_cache / Parquet: none.
dry_run: stored but not consulted (GET-only, non-mutating).

Singleton / concurrency: standard `BaseManager` caching; synchronous `requests` with a default 5-second timeout. No retries, no connection pooling beyond `requests` defaults.

Wiring: loaded as the NON-critical `api_factory` component by `SonarrValidatorManager`. Because it is non-critical, a failure to construct it does not flip the validator subtree's aggregate "loaded" flag.

## How it functions

Lifecycle: `__init__` (resolve manager + dual cache + headers, register) → callers invoke `get_raw` / `get_instance_raw` on demand. There is no `run()` entry point; it is a utility object.

`get_instance_raw` is the convenience layer: it accepts a heterogeneous instance dict (tolerating either `base_url`/`url` and `api`/`api_key`/`token` key spellings), assembles the canonical `/api/v3/` URL, attaches the `X-Api-Key` auth header, and forwards to `get_raw`. Both methods swallow exceptions and return `{}` so callers can treat "unreachable" and "empty" uniformly.

No machine_learning delegation.

## Criteria & examples

- URL assembly trims a trailing slash off the base and a leading slash off the endpoint, so `base_url="http://localhost:8989/"` + `endpoint="/system/status"` → `http://localhost:8989/api/v3/system/status`.
- Token resolution prefers `api`, then `api_key`, then `token`. Example: `instance_obj = {"url": "http://localhost:8989", "api": "abc123"}` → header `{"X-Api-Key": "abc123"}`.
- Any non-2xx (via `raise_for_status`) or transport error returns `{}` rather than raising. Example: a 401 from a bad key logs `⚠️ Failed raw GET to .../system/status: ...` and returns `{}`, which a caller like the health probe would read as "not healthy."

## In plain English

This is a no-frills errand runner. When another inspector needs to quickly ask a Sonarr server a single question over the web — "what's your status?" — but doesn't want to set up the full formal phone system, it hands this helper the address and the door key (the API key), and the helper goes, knocks, brings back whatever the server said, and if anything goes wrong it just shrugs and comes back empty-handed instead of causing a scene. It only ever *reads* — it never changes anything on the server.

## Interactions

- **Parent manager:** loaded as `api_factory` (non-critical) by `SonarrValidatorManager`.
- **Consumers:** validator submodules needing raw `/api/v3` GETs (per the docstring, e.g. status checks).
- **External:** the `requests` library directly; Sonarr `/api/v3/<endpoint>` HTTP endpoints.
- **Brain modules:** none.
