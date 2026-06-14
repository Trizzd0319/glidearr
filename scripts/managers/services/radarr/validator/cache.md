# RadarrValidatorCacheManager

- **File** — `scripts/managers/services/radarr/validator/cache.py`
- **One-liner** — Checks that a fixed set of required Radarr cache keys exist per instance, and lazily warms (re-fetches) any that are missing.

## What it does (for a senior Python engineer)

`RadarrValidatorCacheManager(BaseManager, ComponentManagerMixin)` is a validation leaf under `RadarrValidatorManager`. It guards against a half-populated `global_cache` by verifying a known list of per-instance cache keys exists; for any that are absent it does a FETCH+CACHE warm-up via the shared Radarr client.

Position in the tree: `parent_name = "RadarrValidatorManager"`. No submanagers loaded. `BaseManager` singleton with the usual injected deps plus `self.radarr_api`, `self.instance_manager`, `self.dry_run` resolved in `__init__`.

The required-key contract is the class constant `REQUIRED_KEYS`:
`radarr.instance.metadata`, `radarr.instance.health`, `radarr.history`, `radarr.tags`, `radarr.quality_profiles`, `radarr.custom_formats`, `radarr.quality_definitions`, `radarr.space_estimates`. Each is checked per instance as `f"{key}.{instance}"`.

- **FETCH**: yes (only during warm-up). **CACHE**: yes — writes warmed data back to `global_cache`. **APPLY**: none against the Radarr API.
- **Public methods**:
  - `validate_all_instances()` — loops `_get_all_instances()` and calls `validate(instance)` for each.
  - `validate(instance: str)` — for every `REQUIRED_KEYS` entry, builds `full_key = f"{key}.{instance}"` and tests presence via `global_cache.exists(full_key)` if available, else `global_cache.get(full_key, default=None) is not None`. Missing keys are logged and collected; if none missing it logs all-present, otherwise it logs a "N keys checked, M missing → warming cache" line and calls `_warm_cache_for_validation`.
- **Internal helpers**:
  - `_get_all_instances() -> list` — instance names from `radarr_api.get_all_radarr_apis().keys()`, fallback `instance_manager.get_all_radarr_apis().keys()`, else `[]` (guarded).
  - `_warm_cache_for_validation(instance, missing_keys: list)` — for each missing key it derives an API endpoint from the key's tail (`key.split(".")[-1].replace("_", "/")`), calls `radarr_api._make_request(instance, endpoint, fallback={})`, and writes the result to `global_cache.set(full_key, data)`. Per-key failures are caught and warned.
- **External API endpoints**: derived dynamically from key tails during warm-up — e.g. `quality_profiles` → `quality/profiles`, `custom_formats` → `custom/formats`, `quality_definitions` → `quality/definitions`, `space_estimates` → `space/estimates`, `history` → `history`, `tags` → `tags`, `metadata` → `metadata`, `health` → `health`. (NB: the endpoint is derived from the last dot-segment only, so `radarr.instance.metadata` warms from endpoint `metadata` and `radarr.instance.health` from `health` — the `instance` middle segment is dropped. Whether each derived endpoint is a real Radarr v3 route is not validated by this code.)
- **Config keys read**: none directly.
- **global_cache keys read**: all eight `REQUIRED_KEYS` suffixed with `.{instance}`.
- **global_cache keys written**: any of those eight that were missing (warm-up), same `f"{key}.{instance}"` form.
- **dry_run**: captured but **not honored** — `_warm_cache_for_validation` calls `global_cache.set` unconditionally even in dry-run. This is a cache write (not a Radarr mutation), so it does not change remote state, but it is worth noting it ignores `self.dry_run`.
- **Singleton / concurrency**: `BaseManager` singleton; sequential per-instance/per-key, no threading.

## How it functions

Lifecycle: `__init__` → `super().__init__` → `register()` → resolve deps → debug log. No `load_components`. At runtime a caller invokes `validate_all_instances()` (or `validate(instance)`): discover instances → for each, test all eight required keys → warm any missing by re-fetching the derived endpoint and caching the response. No `machine_learning` brain module is involved — this is a mechanical presence-check plus a re-fetch.

## Criteria & examples

- A key is considered present if `global_cache.exists(full_key)` is True (or, absent an `exists` method, if `global_cache.get(full_key)` is not None).
- Example (instance `"1080"`): of the eight required keys, `radarr.quality_profiles.1080` and `radarr.tags.1080` are missing. `validate("1080")` logs two "Missing cache key" warnings, then "8 keys checked, 2 missing → warming cache". `_warm_cache_for_validation` derives endpoints `quality/profiles` and `tags`, fetches each via `_make_request("1080", ...)`, and writes `radarr.quality_profiles.1080` and `radarr.tags.1080` back into `global_cache`.
- Example (all present): `validate("4k")` logs "All required Radarr cache keys present for '4k'." and performs no fetches or writes.

## In plain English

Before the system tries to make decisions about your movie library, it needs its reference binders stocked: the list of quality profiles, the tags, the history, how much space things take, and so on. This inspector walks the shelf for each Radarr library and checks that all eight binders are there. If a binder is missing, it doesn't just complain — it phones Radarr, fetches a fresh copy, and slots it back on the shelf so the rest of the system isn't working blind. It's like a librarian who notices a missing reference book and quietly re-orders it before anyone needs it.

## Interactions

- **Parent manager**: `RadarrValidatorManager`.
- **Sibling submanagers**: `RadarrValidatorAuthManager`, `RadarrValidatorHealthManager`, `RadarrValidatorKeysManager`.
- **Other services**: the shared `radarr_api` client (`_make_request`, `get_all_radarr_apis`), `instance_manager` (fallback discovery), and `GlobalCacheManager` (`exists`/`get`/`set`).
- **Brain modules**: none.
