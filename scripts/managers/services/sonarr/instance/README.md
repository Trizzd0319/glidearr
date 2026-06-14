# SonarrInstanceManager

- **File** — `scripts/managers/services/sonarr/instance/__init__.py`
- **One-liner** — The canonical `sonarr_api` adapter: it validates every configured Sonarr instance, builds an `arrapi` REST client per instance, and is the single object the rest of the app routes Sonarr HTTP through.

## What it does (for a senior Python engineer)

`SonarrInstanceManager` subclasses `BaseInstanceManager` (which itself is `BaseManager` + `ComponentManagerMixin`). It fills in the four-method subclass contract that `BaseInstanceManager` expects so the shared machinery knows it is operating on Sonarr:

- `_api_class()` → `arrapi.SonarrAPI` (imported as `ArrapiSonarrAPI`).
- `_config_key()` → `"sonarr_instances"` — the config dict that holds the instances.
- `_apis_attr()` → `"sonarr_apis"` — the attribute name of the dict holding validated clients.
- `_service_name()` → `"Sonarr"` — display string used in log lines and lock/cache keys.

It does NOT use `load_components`. Instead `__init__` eagerly constructs exactly two child managers and stores them as attributes:

- `self.repair = SonarrRepairInstanceManager(**shared)`
- `self.updater = SonarrInstanceUpdaterManager(**shared)` (documented in `updater.md`).

The `shared` kwargs dict forwards `logger`, `config`, `global_cache`, `validator`, `registry`, the Sonarr-specific `sonarr_cache`, `manager=self`, and crucially `dry_run`. (See the dry_run note below.)

**Where it sits in the manager tree.** Its parent is `SonarrManager` (`scripts/managers/services/sonarr/__init__.py`). The parent builds it EAGERLY in its own `__init__` (not through the lazy `component_map`) precisely because every other Sonarr submanager — `storage`, `series`, `episodes`, `monitoring`, `repair`, `validator_manager` — declares a dependency on `instance_manager`. After construction the parent does `self.sonarr_api = self.instance_manager` and `self.instance_manager.sonarr_api = self.instance_manager`, then `set_sonarr_cache(...)`. So this object IS the Sonarr `sonarr_api` reference the whole subtree shares.

**FETCH / CACHE / APPLY.** It is squarely the FETCH-enabling layer plus instance-config APPLY:
- FETCH: it builds the validated `arrapi` clients that all Sonarr HTTP GETs flow through, and `BaseInstanceManager._make_request` (inherited) is the routed request entry point.
- APPLY (config side, indirect): through `self.updater.apply_corrections(...)` it writes `failed` flags back into `config.json` for instances that fail or recover. It does not itself PUT/DELETE library content.

**External API endpoints touched.** Validation calls `arrapi`'s `api.system_status()` (the Sonarr `/system/status` endpoint) for each instance to confirm reachability + read the version string. Inherited `_make_request` / `disk_free_*` helpers can reach `series`, `rootfolder`, `diskspace`, etc., but this file's own logic only triggers `system_status`.

**Config keys read.** `sonarr_instances` (the dict of `{name: {url/base_url, api, port, ssl, failed?}}`, plus a special `default_instance` string key). `dry_run` is read indirectly as a kwarg, not from config here.

**global_cache / Parquet keys.** None written directly in this file. It holds references to `global_cache` and a Sonarr-specific `sonarr_cache` and propagates them to its children via `set_sonarr_cache`.

**Registry flag.** `_finalize` sets `sonarr.instance_manager_initialized` to whether all validated instances came up green.

**dry_run behavior.** `BaseManager.__init__` does NOT capture `dry_run`, so this class explicitly reads `self.dry_run = kwargs.get("dry_run", False)` and threads it into the `shared` dict for `repair` and `updater`. This is the documented "dry_run propagation footgun": without this line the children would silently default to `dry_run=False` and could write live.

**Singleton / concurrency notes.** As a `BaseManager` it is a process-wide singleton keyed by `(class, singleton_key)`. The inherited `_make_request` serializes writes through process-wide per-`(service, instance)` locks (`_WRITE_LOCKS`) and memoizes the heavy `series` collection GET per run, invalidating on any write — but those live in the base class, not here.

### Public methods

- `run()` — the deferred validation entry point (called during `SonarrManager.prepare`, not at construction). See "How it functions".
- `get_all_sonarr_apis()` → the `{name: arrapi_client}` dict.
- `get_sonarr_api(name)` → one client by name, or `None`.
- `get_all_instance_names()` → list of validated instance names.
- `get_default_api()` → the sole client if only one is configured, else the client named by `default_instance`.
- `get_default_instance()` → `{"name": ...}` resolving (in order) the configured `default_instance` string, the first validated client, or the first non-`default_instance` config entry; `{}` if none.
- `resolve_instance(name=None)` → returns `name` if it is a non-empty string, else the default instance's name. This is the override `BaseInstanceManager._resolve_instance_name` prefers.
- `get_client(instance_name)` → `get_sonarr_api(resolve_instance(instance_name))` — the convenience "give me the right client" call.
- `set_sonarr_cache(cache_manager)` → stores the cache and forwards it to `repair`/`updater` children that expose `set_sonarr_cache`.

## How it functions

1. **Init.** Sets `parent_name`, calls `super().__init__` (wiring shared deps via `BaseManager`), resolves `sonarr_cache`/`global_cache` from kwargs or the parent manager, calls `self.register()`, initializes `self.sonarr_apis = {}` and `self.load_summary = {}`, captures `dry_run`, then eagerly builds `self.repair` and `self.updater`, and finally sets `self.sonarr_api = self` so it is the canonical API ref even before the parent re-points it.

2. **run() control flow** (deferred validation):
   - `_credential_bootstrap()` — constructs a transient `SonarrRepairInstanceCredentialsManager` and runs it in `mode="bootstrap"`. If it returns `success=False`, `run()` logs the abort, sets `all_components_loaded = False`, and returns early (no instances validated).
   - `self.repair.run()` — runs the repair instance manager.
   - Reads `sonarr_instances` from config and seeds `self.updater.apply_corrections({name: "success"})` for every key, getting back the (possibly correction-applied) instance dict.
   - For each corrected instance (skipping the `default_instance` key), calls the inherited `_process_instance(name, cfg)`. If it returns `"recovered"`, calls `_confirm_and_clear_failed_flag(name, cfg)` to re-validate and clear the `failed` flag.
   - `_finalize(service_name="Sonarr", flag_key="sonarr.instance_manager_initialized")` — sets the registry flag and emits the one-line `[SonarrInstanceManager] ✅ N/M: ...` summary.

3. **Inherited heavy lifting.** `_process_instance`, `_handle_interactive_correction`, `_confirm_and_clear_failed_flag`, `_make_request`, the SQLite-busy retry policy, and the mount-deduped `disk_free_*`/`disk_total_*` helpers all live in `BaseInstanceManager`. This subclass only supplies the Sonarr identity and the `run()` orchestration.

**Brain delegation.** None. This is a pure adapter — no `machine_learning/` decision is delegated from this file.

## Criteria & examples

- **Single-instance shortcut.** `get_default_api()`: if `len(self.sonarr_apis) == 1`, it returns that one client directly without consulting `default_instance`. Example: a homelab with just `"main"` configured always resolves to `main` even if `default_instance` is unset.
- **Default resolution fallback chain** (`get_default_instance`): config `sonarr_instances = {"default_instance": "tv", "tv": {...}, "anime": {...}}` → returns `{"name": "tv"}`. If `default_instance` were missing but `sonarr_apis` had `anime` validated first, it returns `{"name": "anime"}`. If nothing is validated yet, it falls to the first non-`default_instance` config key.
- **Recovery path** (in `run`): an instance whose validation returns `"recovered"` (i.e. the user re-entered a key/host interactively in `_handle_interactive_correction`) then gets `_confirm_and_clear_failed_flag` — a second `system_status()` call. If that succeeds the `failed` flag is popped and `apply_corrections({name: "success"})` persists the clean state; if it fails again `failed=True` is re-set.
- **Credential gate.** If `_credential_bootstrap` fails (e.g. no API key obtainable), zero instances are validated and `all_components_loaded` is set `False` — downstream Sonarr submanagers will find an empty `sonarr_apis`.

## In plain English

Think of this as the front desk for your Sonarr TV servers. Before the app tries to do anything with your shows — say, make sure the next season of *The Mandalorian* is downloading — the front desk phones each Sonarr server, checks the key still works and the door is open (`system_status`), and keeps a ring of working keys (`sonarr_apis`). If a key is wrong it asks you for a new one and tries again; if a server stays unreachable it writes a sticky "this one is broken" note so the app doesn't keep banging on a dead door. Everyone else in the building who needs to talk to Sonarr comes to this front desk and asks "which key do I use for *that* server?" — that's what `get_client`/`resolve_instance` answer.

## Interactions

- **Parent manager:** `SonarrManager` (builds it eagerly, then sets it as the shared `sonarr_api`).
- **Child managers it builds:** `SonarrInstanceUpdaterManager` (`self.updater`, persists `failed` flags) and `SonarrRepairInstanceManager` (`self.repair`); a transient `SonarrRepairInstanceCredentialsManager` is created during `_credential_bootstrap`.
- **Base class:** `BaseInstanceManager` provides validation, request routing, write-locking, collection memo, and disk-space helpers.
- **Consumers (siblings):** `storage`, `series`, `episodes`, `monitoring`, `repair`, `validator_manager`, `sync/tags`, `quality/selector`, etc., all receive this object as `instance_manager` and call `get_all_sonarr_apis` / `resolve_instance` / `get_client`.
- **External service:** Sonarr via the `arrapi` library.
- **Brain modules:** none (pure adapter).
