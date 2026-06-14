# BaseInstanceManager

**File** — `scripts/managers/factories/base_instance_manager.py`
**One-liner** — The shared base for the Radarr/Sonarr (`*arr`) instance managers: it validates configured instances, routes every raw REST request through one chokepoint (`_make_request`) with write-serialization, SQLite-busy retries, and a run-scoped collection snapshot memo, and exposes mount-deduped free/total disk-space helpers.

## What it does (for a senior Python engineer)

`BaseInstanceManager(BaseManager, ComponentManagerMixin)` centralizes the plumbing every `*arr` instance manager needs so the concrete Radarr/Sonarr managers stay thin. It is abstract: subclasses MUST implement `_api_class()`, `_config_key()`, `_apis_attr()`, and (optionally) `_service_name()`.

In FETCH/CACHE/APPLY terms it is the **transport layer for all three**: `_make_request` issues the HTTP GET (FETCH), POST/PUT/DELETE (APPLY the *arr-side decision), and maintains the in-process collection snapshot memo (a transient CACHE — see below). It does not, itself, decide *what* to write; it executes whatever endpoint/method/payload a caller hands it. It does not read/write the persistent `global_cache`/Parquet store directly. There is no explicit `dry_run` branch here — in dry-run mode upstream callers simply never issue writes, which the snapshot memo relies on.

### Subclass interface

- `_api_class()` → the `arrapi` class (e.g. `RadarrAPI`). **Required.**
- `_config_key()` → config dict key holding the instance configs (e.g. `"radarr_instances"`). **Required.**
- `_apis_attr()` → name of the attribute holding the dict of validated API clients (e.g. `"radarr_apis"`). **Required.**
- `_service_name()` → display string for logs (defaults to the class name).

### Instance validation lifecycle

- `_process_instance(name, config)` → `"success" | "recovered" | "fail"`. Skips instances flagged `failed` in config (returns `"fail"`, marks `load_summary[name] = "❌"`). Otherwise parses the host, constructs `self._api_class()(base_url, config.get("api"))`, calls `api.system_status()` to read the version, injects a default timeout, stores the API via `_set_api`, and marks `load_summary[name] = "✅"`. On exception it routes into `_handle_interactive_correction`.
- `_handle_interactive_correction(name, config, error_msg, protocol, port)` → `"recovered" | "fail"`. On a `401`/`Unauthorized` it prompts (`getpass`) for a new API key; on a connection failure it prompts for a new host + port; then it retries `system_status()`. Persists `config["failed"] = True` and notifies `self.updater.apply_corrections({name: "fail"})` if the retry fails. (Interactive — blocks on `getpass`/`input`.)
- `_confirm_and_clear_failed_flag(name, config)` — Re-validates after correction; clears the `failed` flag on success or re-sets it on failure, notifying `self.updater` either way.
- `_finalize(service_name, flag_key)` — Sets `self.all_components_loaded`, writes the registry flag via `self.registry.set_flag(flag_key, all_ok)`, and logs one compact `[Cls] ✅ n/total: name✅ …` summary line (same format as `ComponentManagerMixin`).
- `prepare()` — Overridden **no-op**: instance managers have no subcomponents to prepare.

### `_make_request` — the single REST chokepoint

`_make_request(instance, endpoint, method="GET", payload=None, fallback=None, retries=1, **kwargs)`:

- Resolves the instance name (`_resolve_instance_name`), looks up the validated `arrapi` client in `self.<_apis_attr()>`; returns `fallback` if there is none.
- Dispatches on method to the raw client: GET → `raw._get(endpoint)`, POST → `raw._post(endpoint, json=payload)`, PUT → `raw._put(endpoint, json=payload)`, DELETE → `raw._delete(endpoint)` (returns `None`). Unknown methods return `fallback`.
- **Write serialization:** POST/PUT/DELETE acquire a process-wide per-`(service, instance)` `threading.Lock` (`_write_lock_for`) **only for the call itself**, never during backoff sleeps. This guarantees Glidearr never has two of its own writes in flight against the same single-writer SQLite *arr DB — crucially the JIT background worker vs. the main pipeline.
- **SQLite-busy retry:** if a call raises a "database is locked"/`SQLITE_BUSY`/`code = busy` error (`_is_db_locked`), it retries with exponential backoff + ±20% jitter (`_sqlite_backoff_delay`), up to `_SQLITE_BUSY_MAX_RETRIES = 8` (base `0.25s`, per-attempt cap `8.0s`, ~30s ceiling). The write never committed, so a retry is safe.
- Returns the result, or `fallback` when the result is `None`/all attempts fail.

### Run-scoped collection snapshot memo

Module-level state: `_COLLECTION_CACHE` (`(service, instance, endpoint) -> (monotonic_ts, list)`), `_COLLECTION_CACHE_GEN` (`(service, instance) -> int` write generation), guarded by `_COLLECTION_CACHE_GUARD`, with `_CACHEABLE_GET_ENDPOINTS = {"movie", "series"}` and a `_COLLECTION_CACHE_TTL_S = 900s` safety backstop.

- The heavy full-library GETs `movie` (Radarr) and `series` (Sonarr) are re-fetched 13+ times per run by separate scans (~268s / 77% of a dry-run wall on a ~20k-item library). They are memoized **exact-match only** at this chokepoint.
- On a cacheable GET, `_collection_cache_lookup` returns a fresh `list()` copy of the snapshot (list-level mutation is safe; inner dicts stay shared and must be treated read-only) plus the captured generation; a hit skips the network entirely.
- Any write to `(service, instance)` calls `_collection_cache_invalidate`, which **drops both snapshots and bumps the generation**, so the next GET re-fetches live.
- `_collection_cache_store` only persists a snapshot if the generation is unchanged since the lookup (`gen0`), closing the store-after-invalidate race (a write landing mid-fetch rejects the stale store).
- `_clear_collection_cache()` resets everything at teardown.
- **Invariants (documented in source):** the whitelist is EXACT-MATCH — never add a volatile/action/parameterized endpoint (`command`, `queue`, `system/status`, `rootfolder`, `diskspace`, `movie/{id}`, `series?page=…`, etc.); writes to `movie`/`series` MUST go through *this* `_make_request` to invalidate (raw `arrapi` writes bypass it).

### Free/total disk-space helpers (mount-deduped)

Root folders sharing one physical disk each report that disk's *full* free space, so naive summation double-counts. These helpers dedupe by the underlying `/diskspace` mount.

- `disk_free_bytes(instance)` / `disk_free_gb(instance)` — Free space; matches each root folder to its longest-prefix `/diskspace` mount (`_path_under_mount` uses a separator boundary so `/data` ≠ `/database`), sums one free value per chosen mount. Falls back to value-deduped `rootfolder.freeSpace` when `/diskspace` is unavailable. Returns `float('inf')` on error or when there are no root folders ("assume sufficient" contract). `_gb` divides by `1024**3` (GiB).
- `disk_total_bytes(instance)` / `disk_total_gb(instance)` — Same logic for `totalSpace`.
- Path normalization (`_norm_path`): lower-cased, forward-slashed, trailing-slash stripped, bare root kept as `"/"`.

### External API endpoints touched

Via `arrapi`'s raw client: `system_status` (validation), `rootfolder` (GET), `diskspace` (GET), and the cacheable full-library `movie`/`series` GETs — plus whatever endpoint/method any caller passes through `_make_request` (e.g. POST `command` for `EpisodeSearch`).

### Config keys read

The instance-config block named by the subclass's `_config_key()` (e.g. `radarr_instances` / `sonarr_instances`). Per-instance entries read here: `base_url`, `url`, `port`, `ssl`, `api`, and the `failed` flag.

### Threading / concurrency notes

- `_WRITE_LOCKS` (per `(service, instance)`, guarded by `_WRITE_LOCKS_GUARD`) serialize Glidearr's own writes per single-writer SQLite DB; distinct instances/services don't contend.
- `_COLLECTION_CACHE` / `_COLLECTION_CACHE_GEN` (guarded by `_COLLECTION_CACHE_GUARD`) are process-wide; the generation counter makes the memo correct under the concurrent JIT-worker-vs-pipeline case.
- Inherits `BaseManager`'s `(cls, singleton_key)` singleton semantics.

## How it functions

Init follows the `BaseManager` path (deps injected, registered under `"manager"`, parent-linked). The concrete subclass populates its `_apis_attr()` dict by iterating its `_config_key()` instances through `_process_instance` (each validated against `system_status`, with interactive correction on failure), tracking results in `load_summary`, then calls `_finalize(service, flag_key)` to publish the registry flag and the summary line. `prepare()` is a no-op.

At steady state everything funnels through `_make_request`: reads of `movie`/`series` ride the run-scoped memo, reads of `rootfolder`/`diskspace` feed the space helpers, and every write takes the per-instance lock, invalidates the memo, and retries on SQLite-busy. Mixing in `ComponentManagerMixin` gives subclasses `load_components(...)` if they ever attach submanagers (the base itself loads none).

This module delegates **no** decisions to a `machine_learning` brain module — it is the transport/validation/space-accounting layer. The value-judgements (what to delete, downgrade, search) are computed by `machine_learning/` brains and *applied* through this class's `_make_request` by the service managers above it.

## Criteria & examples

- **Snapshot memo (dry-run):** First GET of `movie` on instance `radarr-main` misses, fetches the live ~20k-item list, and stores it; every later identical `movie` GET in the same run returns a `list()` copy without touching the network. With zero writes (dry-run), this holds for the whole run.
- **Memo invalidation:** A PUT to `movie/editor`… is *not* whitelisted, but a write to bare `movie` (or any write to `radarr-main`) bumps `_COLLECTION_CACHE_GEN[("Radarr","radarr-main")]` from, say, `3 → 4` and drops both snapshots; a fetch that read `gen0 = 3` then tries to store is rejected (`4 != 3`), so no stale snapshot is persisted.
- **SQLite-busy backoff:** A POST `command` (EpisodeSearch) that returns "database is locked" retries: attempt 1 ≈ `0.25s × (0.8–1.2)`, attempt 2 ≈ `0.5s`, … capped at `8.0s` each, up to 8 tries (~30s total) before giving up and returning `fallback`.
- **Mount dedup:** Two root folders `/data/movies` and `/data/tv` both under `/diskspace` mount `/data` (free 500 GB). `disk_free_bytes` matches both to `/data` (longest prefix), records `/data → 500 GB` once, and returns `500 GB` — not `1000 GB`.
- **Separator boundary:** A root `/database/x` will NOT be attributed to a mount `/data` because `_path_under_mount` requires equality or a `/`-boundary prefix (`/data/`).
- **No root folders:** `disk_free_gb` returns `float('inf')` ("assume sufficient"), so space-pressure logic never blocks on a misconfigured instance.

## In plain English

Imagine your media server is a big warehouse, and Glidearr keeps asking the front desk, "give me the full catalog of every movie we own." That catalog is huge and slow to print. The first time, the desk prints it; for the rest of the visit it just hands back photocopies of that same printout — unless someone actually changes the shelves, in which case it throws the old printout away and prints a fresh one. That's the snapshot memo, and it turns out to cut roughly three-quarters of the wait on a big library.

It also acts like a polite traffic cop at the warehouse's single loading dock: only one of Glidearr's own change-requests (add this **Toy Story** sequel, delete that duplicate **Die Hard**) can be at the dock at a time, and if the dock is briefly jammed it waits a moment and tries again instead of giving up. And when it tots up free disk space, it's smart enough to know that two shelves in the same room share the same floor space, so it doesn't accidentally claim you have twice the room you really do.

## Interactions

- **Parent manager:** `BaseManager` (provides singleton identity, dep injection, registry registration, parent-linking). Mixes in `ComponentManagerMixin` for `load_components`.
- **Subclasses:** the concrete Radarr and Sonarr instance managers, which supply `_api_class`/`_config_key`/`_apis_attr`/`_service_name`. These instance managers are themselves the `*_api`/`instance_manager` collaborators referenced throughout the service-manager tree.
- **External services:** the `arrapi` clients for each *arr instance (HTTP to Radarr/Sonarr REST), reached through every `_make_request` call.
- **Collaborators:** `self.updater` (when present) receives `apply_corrections({name: status})` after validation/correction; `self.registry` receives the readiness flag via `_finalize`; `self.logger` emits validation/summary/redacted-error lines (`_redact_error` strips API keys, 32-hex keys, UUIDs, and Bearer tokens from exception strings).
- **Brain modules:** none directly. Decisions originate in `machine_learning/` and are *carried out* through this class by the service managers above it.
