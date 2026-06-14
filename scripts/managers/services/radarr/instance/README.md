# RadarrInstanceManager

- **File** — `scripts/managers/services/radarr/instance/__init__.py`
- **One-liner** — The `radarr_api` reference object: it validates every configured Radarr instance, holds the live `arrapi` clients, and is the single freshness-gated entry point for the whole movie library.

## What it does (for a senior Python engineer)

`RadarrInstanceManager(BaseInstanceManager)` is the canonical Radarr API adapter. `BaseInstanceManager` itself is `BaseManager + ComponentManagerMixin`, so this class is a process-wide singleton with the shared logger/config/global_cache/validator/registry deps injected.

It fills in the four abstract hooks `BaseInstanceManager` requires:
- `_api_class()` → `arrapi.RadarrAPI`
- `_config_key()` → `"radarr_instances"`
- `_apis_attr()` → `"radarr_apis"` (the dict attr that holds validated clients)
- `_service_name()` → `"Radarr"`

**Where it sits in the tree.** Parent is `RadarrManager` (`scripts/managers/services/radarr/__init__.py`), which constructs it eagerly as Step 2 (`self.instance_manager = RadarrInstanceManager(..., manager=self, dry_run=self.dry_run)`) and then aliases `self.radarr_api = self.instance_manager`. Inside its own `__init__`, this manager also sets `self.radarr_api = self` so it is its own canonical API reference — i.e. every downstream Radarr submanager (cache, storage, movies, quality, monitoring, sync, repair) reaches HTTP through this object. It loads exactly one submanager itself, built directly (not via `load_components`): `self.updater = RadarrInstanceUpdaterManager(...)` (see `updater.md`).

**FETCH / CACHE / APPLY.** Primarily **FETCH** (HTTP GET via the inherited `_make_request`) and **CACHE** (it persists the full movie list snapshot to `global_cache` and stamps a freshness timestamp). It performs no value judgement and delegates no decision to a `machine_learning` brain module. APPLY (PUT/DELETE/POST) for movies happens in other Radarr submanagers, but it routes through this object's inherited `_make_request`, which is what keeps the collection-cache invalidation correct.

**Public methods.**
- `get_all_radarr_apis() -> dict` — returns the `{instance_name: arrapi_client}` map.
- `get_radarr_api(instance_name)` — one client by exact name (or `None`).
- `get_default_instance() -> dict` — `{"name": ...}` resolving (in order) the configured `radarr_instances["default_instance"]` (a `{"name": <instance>}` dict, or a bare string), the first validated client, or the first non-`default_instance` config entry; `{}` if none. Mirrors `SonarrInstanceManager.get_default_instance`.
- `resolve_instance(name=None) -> str` — name resolution: returns `name` if it is a known instance; else the configured default if known; else the first instance; else `name or "default"`. Overrides the base `_resolve_instance_name` fallback.
- `get_client(instance_name)` — the resolved client (`resolve_instance` + lookup).
- `get_movie_library(instance, max_age_s=0, global_cache=None)` — the freshness-gated full movie list (see below).

**External API endpoints touched (via `_make_request`).** `GET /movie` (the full library), plus on validation `system_status()` (an `arrapi` call). The inherited free-space helpers touch `GET /rootfolder` and `GET /diskspace`. Writes for movies happen elsewhere but flow through this object.

**Config keys read.** `radarr_instances` (the instance map; iterated in `__init__`, with the special `default_instance` key skipped). `get_default_instance` reads the same map's `default_instance` marker (the `{"name": <instance>}` dict).

**global_cache / timestamp keys.**
- Read/written: `radarr.movies.{instance}.full` — the persisted full movie-list snapshot.
- Timestamp handler key: `("radarr", instance, "movie_library")` via `gc.timestamp_handler.is_fresh / get_age_seconds / update_timestamp`.
- Registry flag set at finalize: `radarr.instance_manager_initialized`.

**dry_run behavior.** `dry_run` arrives as a kwarg from `RadarrManager` but `BaseManager` does not capture it, so `__init__` explicitly stores `self.dry_run = kwargs.get("dry_run", False)` and threads it into the `updater` child (otherwise the child would silently default to `False` and could write live — this is the documented dry_run-propagation footgun). `get_movie_library` is read-only against Radarr, so it behaves identically in dry_run; the cache layer is best-effort and a cache error never breaks a fetch.

**Singleton / concurrency.** Singleton via `BaseManager`. The heavy `GET /movie` collection is memoized process-wide in the base class's run-scoped collection cache (`_COLLECTION_CACHE`), guarded by a lock and invalidated/generation-bumped on any write to `(service, instance)`. Writes serialize through a per-`(service, instance)` `_WRITE_LOCKS` lock in the base. This manager's own contribution is the disk-snapshot freshness gate layered on top.

## How it functions

Lifecycle inside `__init__`:
1. `super().__init__(...)` wires shared deps and auto-links the parent; `self.register()` self-registers in the registry.
2. Initialise `self.radarr_apis = {}` and `self.load_summary = {}`, capture `self.dry_run`.
3. Build `self.updater` (the `RadarrInstanceUpdaterManager`), passing `manager=self` and the captured `dry_run`.
4. Read `config.get("radarr_instances", {})`, pre-seed all names as `"success"`, and call `self.updater.apply_corrections(...)` to normalise the config before validation.
5. For each instance (skipping the `default_instance` key), call the inherited `_process_instance(name, cfg)`. On `"recovered"`, call `_confirm_and_clear_failed_flag(name, cfg)` to re-validate after an interactive credential/host fix. `_process_instance` does the actual `system_status()` probe, timeout injection, and stores the client via `_set_api`.
6. Set `self.radarr_api = self`, then `_finalize(service_name="Radarr", flag_key="radarr.instance_manager_initialized")`, which computes `all_components_loaded`, sets the registry flag, and emits the one-line `[RadarrInstanceManager] ✅ n/m: ...` summary.

`get_movie_library` is "the single freshness decision for the whole run." Order of resolution:
1. In-process collection memo (`_collection_cache_lookup`) — returns a list copy if warm.
2. If `max_age_s` is set and a `global_cache` + `timestamp_handler` exist, and `("radarr", instance, "movie_library")` is fresher than `max_age_s`, read `radarr.movies.{instance}.full` from disk, warm the in-process memo so later bare `GET /movie` calls (repair scans, orchestration enrichment — all funnel through `_make_request`) reuse it, log the skip, and return the snapshot.
3. Otherwise live-fetch `_make_request(instance, "movie", fallback=[])`, then re-persist `radarr.movies.{instance}.full` and re-stamp the timestamp for next run.

`max_age_s=0` (or no cache/timestamp handler) always fetches live — today's default behavior. This mirrors Sonarr's `series_library` gate. No `machine_learning` delegation occurs in this manager.

## Criteria & examples

- **Skip the `default_instance` config key.** Iterating `radarr_instances`, the literal key `"default_instance"` is `continue`d so it is never treated as a real instance.
- **Freshness gate.** With `max_age_s=900` (15 min): if the on-disk snapshot for `radarr.movies.main.full` was stamped 8 minutes ago (480s ≤ 900s) and is a non-empty list, the live `GET /movie` is skipped and the disk snapshot returned (e.g. logged as "fresh on disk (age 8m 0s ≤ 900s) — skipped live GET /movie (12,431 movies)"). If it was stamped 20 minutes ago (1200s > 900s), it re-fetches live and re-stamps.
- **`max_age_s=0`.** Always live-fetches regardless of how fresh the disk snapshot is (the gate is opt-in per caller).
- **Recovery path.** A `_process_instance` result of `"recovered"` (interactive credential/host correction succeeded) triggers `_confirm_and_clear_failed_flag`, which does a final `system_status()` probe; success pops the `failed` flag and records `apply_corrections({name: "success"})`, failure re-sets `failed` and records `apply_corrections({name: "fail"})`.
- **Instance resolution precedence.** Given `radarr_apis = {"4k": client_a, "hd": client_b}` and configured default `"hd"`: `resolve_instance("4k")` → `"4k"`; `resolve_instance(None)` → `"hd"`; if the default were unknown it would fall to `"4k"` (the first key).

## In plain English

Think of this object as the front desk and phone line for your Radarr movie server. When the app starts, the front desk calls each Radarr instance you configured ("Are you there? What version?") and keeps a working phone line open for the ones that answer. If a number is wrong it can ask you to retype the credentials, and it remembers which instances are broken so it does not waste time redialing them.

The most useful trick is the movie catalogue. Pulling the full list of every movie (say, all 12,000 titles) is slow, and the app needs that list many times in one run. So the front desk keeps a recent printout: if the printout is younger than the age you allow (e.g. 15 minutes), it hands you the printout instantly instead of phoning the server again; if it is too old, it makes one fresh call, prints a new copy, and stamps the time. Like checking your saved Netflix watchlist instead of reloading the whole site every time you want to glance at it — same answer, far less waiting.

## Interactions

- **Parent:** `RadarrManager` (`scripts/managers/services/radarr/__init__.py`) — constructs it as Step 2 and aliases it as `radarr_api`.
- **Child submanager:** `RadarrInstanceUpdaterManager` (`updater.py`) — applies config corrections / failure-flag updates; built directly in `__init__` and invoked from the inherited validation helpers.
- **Base class:** `BaseInstanceManager` (`factories/base_instance_manager.py`) — provides `_make_request`, `_process_instance`, `_handle_interactive_correction`, `_confirm_and_clear_failed_flag`, the run-scoped collection cache, per-instance write locks, SQLite-busy retry, free-space helpers, and `_finalize`.
- **Sibling Radarr submanagers** (cache, storage, movies, quality, monitoring, sync, repair) — all consume this object as `radarr_api` / `instance_manager` for HTTP.
- **Brain modules:** none. This manager performs no value judgement and delegates no decision to `machine_learning`.
