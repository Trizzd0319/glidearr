# TautulliInstanceManager

- **File** — `scripts/managers/services/tautulli/instances/__init__.py`
- **One-liner** — A registry/factory that turns the `tautulli` config block into named, lazily-built Tautulli instances (each an HTTP `TautulliAPI` + a `TautulliInstanceSummaryFormatter`) and hands callers the API client, a server summary, or a connectivity check by instance name.

## What it does (for a senior Python engineer)

`TautulliInstanceManager(BaseManager)` exists so that the rest of the Tautulli subtree can ask for "the API client for instance X" without each caller having to parse the (two possible) shapes of the `tautulli` config block, build a `TautulliAPI`, or memoize it. It owns one dict, `self.instances`, mapping instance name → `{"api": TautulliAPI, "formatter": TautulliInstanceSummaryFormatter}`, built on first request and cached thereafter.

It supports two config shapes, both rooted at the top-level `tautulli` key:
- **Flat / single-instance** — `{"url": ..., "port": ..., "api": ...}` (all values are strings). Treated as one instance named `"default"`.
- **Multi-instance** — `{"default": {...}, "backup": {...}}` (values are dicts). Each top-level key is an instance name.
The shape is detected by `all(isinstance(v, str) for v in tautulli_config.values())`.

Key PUBLIC methods:
- `get_instance_names() -> list[str]` — Returns the list of configured instance names. For a flat config returns `["default"]`; for a multi-instance config returns `list(tautulli_config.keys())`; returns `[]` if `tautulli` is not a dict.
- `get_instance(name="default") -> dict | None` — The core factory. Returns the cached `{"api", "formatter"}` pair for `name`, building it on first call. Resolves `instance_config` (flat config when `name == "default"` and all values are strings; otherwise `tautulli_config.get(name)`), logs a warning and returns `None` if no config is found, then constructs `TautulliAPI(logger, instance_config, cache=self.global_cache)` and `TautulliInstanceSummaryFormatter(api, logger)` and memoizes them in `self.instances[name]`.
- `get_api(name="default") -> TautulliAPI | None` — Convenience accessor returning just the `api` from `get_instance`, or `None`.
- `get_summary(name="default") -> dict` — Returns the formatter's `format_summary()` result (version / platform / user_count / active_streams), or `{}` if the instance could not be built.
- `validate_instance(name="default") -> bool` — Connectivity check: `api = self.get_api(name); return api and api.validate()`. (`api.validate()` is a `get_server_info` round-trip — see below.)

Where it sits in the manager tree: its parent is **`TautulliManager`** (`scripts/managers/services/tautulli/__init__.py`), which lists it under the `all_component_classes`/`component_dependencies` key **`"instance"`** (singular) and exposes it on the parent as the attribute **`instances`** (plural). It is a non-critical component (not in the parent's `critical_keys`), so it is not loaded during the parent's `prepare()` and is built on demand via the parent's `_singleton(...)`. It does **not** call `load_components` itself and has no submanagers of its own; instead it directly instantiates the plain helper classes `TautulliAPI` and `TautulliInstanceSummaryFormatter`.

FETCH / CACHE / APPLY: this manager is pure plumbing. It performs **no** FETCH, CACHE, or APPLY itself. The objects it hands out do the work — `TautulliAPI` performs FETCH (HTTP GET against the Tautulli JSON API v2), and the formatter triggers a few of those FETCHes. It writes nothing to `global_cache` or Parquet (it only passes `self.global_cache` into `TautulliAPI` as its `cache` argument; `TautulliAPI` as written does not use that cache).

External API endpoints touched (indirectly, via `TautulliAPI`): `get_server_info` (through `validate`), and `get_server_info` + `get_users` + `get_activity` (through `get_summary`). All hit `{base_url}/api/v2?cmd=...`.

Config keys read: the top-level `tautulli` block, and within an instance config `url`, `port`, `api`/`api_key`, and `base_url` (the last four are read inside `TautulliAPI.__init__`).

`global_cache` / Parquet keys: none read or written by this manager.

dry_run behavior: none. This manager only reads config and issues GETs (read-only), so dry_run does not change its behavior.

Singleton / concurrency / threading notes: as a `BaseManager` it is a process-wide singleton (cached in `_instances`). `self.instances` is a plain dict with no locking — `get_instance` is not thread-safe under concurrent first-time builds for the same name, though duplicate builds would simply overwrite the cache entry rather than corrupt it. `TautulliAPI` holds a shared `requests.Session` per instance for connection pooling.

## How it functions

Lifecycle: `__init__` calls `super().__init__(...)` (wiring in the shared logger/config/global_cache/validator/registry and self-registering under the registry "manager" category), then sets `self.instances = {}`. There is no `load_components`, no `prepare`, and no `run` — it is a passive factory queried by other Tautulli managers and by validation paths.

Main control flow is all in `get_instance`: cache hit → return immediately; else resolve the per-instance config (handling flat vs multi-instance), bail with a warning if absent, build `TautulliAPI` + `TautulliInstanceSummaryFormatter`, store the pair, return it. `get_api`, `get_summary`, and `validate_instance` are thin wrappers over `get_instance`.

It delegates **no** decision to any `machine_learning` brain module — there is no value judgement here, only config resolution and client construction.

## Criteria & examples

The only real "rule" is the config-shape discriminator and the cache.

- Flat config `tautulli = {"url": "10.0.0.5", "port": "8181", "api": "abc123"}`: every value is a `str`, so `get_instance_names()` returns `["default"]` and `get_instance("default")` builds a `TautulliAPI` with `base_url = "http://10.0.0.5:8181"`.
- Multi-instance config `tautulli = {"default": {...}, "backup": {...}}`: values are dicts, so `get_instance_names()` returns `["default", "backup"]`. `get_instance("backup")` resolves `tautulli_config.get("backup")`.
- Unknown name: `get_instance("staging")` with no `staging` key → logs `⚠️ Tautulli instance 'staging' not found in config.` and returns `None`; `get_summary("staging")` then returns `{}` and `validate_instance("staging")` returns `False`.
- Memoization: calling `get_api("default")` twice returns the *same* `TautulliAPI` object (and therefore reuses its pooled `requests.Session`), because the second call hits `if name in self.instances`.

## In plain English

Think of this as the front desk of a hotel that may have one branch or several. When some part of the system says "I need to talk to the Plex-stats service called *default* (or *backup*)," the front desk looks up that branch's address and key, builds a phone line to it once, keeps that line on the hook, and hands it back. Ask again later and you get the same line instead of dialing from scratch. It can also give you a quick "is the branch awake and what's its name/version, how many people are watching right now" summary, or just confirm the branch picks up the phone. It never decides anything about your movies — it just makes sure the right connection exists.

## Interactions

- **Parent manager:** `TautulliManager` (registers this under key `"instance"`, exposes it as `.instances`).
- **Helper classes it builds (same directory, not managers):** `TautulliAPI` (`api.py`, the HTTP client) and `TautulliInstanceSummaryFormatter` (`summary_formatter.py`, which calls `get_server_info` / `get_users` / `get_activity` to assemble the summary dict).
- **Sibling submanagers under `TautulliManager`:** `TautulliDevicesManager`, `TautulliEpisodesManager`, `TautulliMetadataManager`, `TautulliSeriesManager`, `TautulliTranscodeManager`, `TautulliUsersManager`, `TautulliWatchHistoryManager`, and `TautulliValidatorManager`. (Note: `TautulliValidatorManager` is a *different* class living at `tautulli/validator.py`, not the `TautulliInstanceValidatorManager` in this directory.)
- **Brain modules:** none — this manager delegates no decisions to `machine_learning/`.
