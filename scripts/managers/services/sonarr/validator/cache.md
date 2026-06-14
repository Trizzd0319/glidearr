# SonarrValidatorCacheManager

**File** — `scripts/managers/services/sonarr/validator/cache.py`
**One-liner** — Verifies that each Sonarr instance has the expected set of cache keys present in the global cache, and (optionally, outside dry-run) warms any that are missing by re-fetching them through the instance's API client.

## What it does (for a senior Python engineer)

`SonarrValidatorCacheManager(BaseManager, ComponentManagerMixin)` is a cache-completeness validator. It sets `self.parent_name = self.__class__.__name__` (i.e. it names itself as its own parent for logging context rather than declaring a fixed parent class).

It defines a fixed required-key list:

```
REQUIRED_KEYS = [
    "sonarr.library",
    "sonarr.episodes",
    "sonarr.series",
    "sonarr.history",
    "sonarr.quality",
    "sonarr.tags",
]
```

State resolved at init:
- Dual cache: `self.sonarr_cache` (from `sonarr_cache` kwarg or parent attr) and `self.global_cache` (the global cache).
- `self.manager` (the `manager` kwarg), `self.dry_run` (from the manager, default `False`), and `self.sonarr_apis` (from the `sonarr_apis` kwarg or the parent's `sonarr_apis` dict — a `{instance_name: api_client}` map).
- Raises `ValueError` if no logger could be resolved.

Public methods:
- `run()` — Iterates `self.sonarr_apis.items()` and calls `_validate_instance_cache(instance_name, api)` for each. No return value.

FETCH / CACHE / APPLY:
- FETCH: only on a warm path — when a required key is missing it calls `api._make_request(instance, key)` to re-pull the raw payload.
- CACHE (write): on a successful warm it writes the result to global cache via `self.global_cache.set(f"{key}.{instance}", result)`.
- It also performs a CACHE-read presence check: `self.global_cache.exists(f"{key}.{instance}")`.

global_cache keys read: existence of `<required_key>.<instance>` for each of the six `REQUIRED_KEYS` (e.g. `sonarr.library.1080`, `sonarr.episodes.4k`, …).
global_cache keys written: `<key>.<instance>` for any missing key successfully warmed.

Config keys read: none directly.
External API: `api._make_request(instance, key)` where `key` is one of the required cache keys (the underlying Sonarr API client maps those to endpoints; this class does not hit raw URLs itself).

dry_run behavior: when `self.dry_run` is true, `_warm_missing_keys` logs `💤 [Dry Run] Would warm: <keys> for <instance>` and returns without fetching or writing anything. Presence checks (`run`/`_validate_instance_cache`) still execute in dry-run.

Singleton / concurrency: standard `BaseManager` process-wide caching; no threading.

Note on wiring: this local validator is NOT the `cache_manager` component loaded by `SonarrValidatorManager` (that slot uses the sibling `SonarrCacheManager` from `scripts/managers/services/sonarr/cache.py`). This class is a standalone validator usable wherever a `sonarr_apis` map and global cache are available.

## How it functions

Lifecycle: `__init__` (resolve dual cache, manager, dry_run, sonarr_apis; register) → caller invokes `run()`.

Control flow of a run:
1. `run()` loops over every `(instance_name, api)` pair.
2. `_validate_instance_cache(instance, api)` builds `full_key = f"{key}.{instance}"` for each of the six required keys, collects any whose `global_cache.exists(full_key)` is false into a `missing` list. If `missing` is empty it logs success; otherwise it logs how many were checked vs missing and calls `_warm_missing_keys`.
3. `_warm_missing_keys(instance, api, keys)` short-circuits in dry-run; otherwise for each missing key it tries `api._make_request(instance, key) or {}`, writes it to global cache, and logs `🔥 Warmed → <key>.<instance>`. Per-key failures are caught and logged as warnings (the loop continues).

No machine_learning brain module is involved — this is mechanical cache hygiene.

## Criteria & examples

- "Missing" is purely an existence test against the six required keys, scoped per instance. Example: instance `1080` has `sonarr.library.1080`, `sonarr.episodes.1080`, `sonarr.series.1080`, `sonarr.history.1080`, `sonarr.quality.1080` present but lacks `sonarr.tags.1080`. `_validate_instance_cache` collects `["sonarr.tags"]`, logs `🧩 6 checked, 1 missing → warming...`, then (non-dry-run) calls `api._make_request("1080", "sonarr.tags")`, writes the response to `sonarr.tags.1080`, and logs the warm.
- Same scenario in dry-run: it logs `💤 [Dry Run] Would warm: ['sonarr.tags'] for 1080` and writes nothing.
- If `_make_request` raises (e.g. the API is down), that one key is logged as `⚠️ Failed to warm key sonarr.tags.1080: ...` and skipped; other instances/keys are unaffected.

## In plain English

Imagine your DVR keeps six little index cards for each TV server — one listing your shows, one your episodes, one the quality settings, and so on. Before the system relies on those cards, this checker flips through them for each server and asks "is this card here?" If a card is missing, it phones the server, asks for the information again, and files a fresh card — unless it's in "practice mode" (dry-run), in which case it just notes "I would re-file the tags card" without actually doing it. The point is to make sure nothing downstream trips over a blank index card and crashes mid-show.

## Interactions

- **Parent / caller:** instantiated with a `manager` that supplies `sonarr_apis`, `dry_run`, and the caches. (It is not the `cache_manager` slot in `SonarrValidatorManager`.)
- **Siblings:** conceptually peer to `SonarrValidatorHealthManager` and `SonarrValidatorKeysManager`, which also probe per-instance state.
- **External:** the per-instance Sonarr API client's `_make_request`; the global cache (`exists` / `set`).
- **Brain modules:** none.
