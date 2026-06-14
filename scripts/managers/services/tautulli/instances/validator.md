# TautulliInstanceValidatorManager

- **File** — `scripts/managers/services/tautulli/instances/validator.py`
- **One-liner** — A one-method manager that confirms a named Tautulli instance is reachable by resolving its config and doing a single `get_server_info` round-trip, with a quiet skip for an absent optional `"backup"` instance.

## What it does (for a senior Python engineer)

`TautulliInstanceValidatorManager(BaseManager)` answers one question: "does the Tautulli instance named `name` exist in config and respond?" It is the connectivity gate used during validation, independent of the full `TautulliInstanceManager` factory.

Key PUBLIC method:
- `validate(self, name="default") -> bool` — Reads the top-level `tautulli` config block, resolves the per-instance config (flat single-instance config when every value is a `str`; otherwise `tautulli_cfg.get(name)`), and:
  - If no config is found: logs `⚠️ Tautulli 'backup' instance not configured — skipping.` at **info** level when `name == "backup"` (an optional instance), otherwise logs `⚠️ Tautulli instance '<name>' not found in config.` at **warning** level — then returns `False`.
  - Otherwise builds a fresh `TautulliAPI(logger=self.logger, instance_config=instance_config)` and returns `bool(api.validate())`, where `api.validate()` issues a `get_server_info` GET and returns the raw response dict (truthy) or `None`.

Where it sits in the manager tree: as a `BaseManager` it self-registers under the registry "manager" category and auto-links to its parent. **It is not wired into `TautulliManager`'s component map** — the parent loads a *different* class, `TautulliValidatorManager` (from `tautulli/validator.py`), under the key `"validator_manager"`. This `TautulliInstanceValidatorManager` is referenced as the "real validation logic" by that parent-level validator shim. It loads no submanagers via `load_components` and has no children of its own; it directly instantiates the plain `TautulliAPI` helper.

FETCH / CACHE / APPLY: performs a single **FETCH** (the `get_server_info` GET, via `TautulliAPI.validate`). No CACHE, no APPLY.

External API endpoints touched (via `TautulliAPI`): `get_server_info` at `{base_url}/api/v2?cmd=get_server_info`.

Config keys read: the top-level `tautulli` block (and, inside `TautulliAPI.__init__`, `url`/`port`/`api`/`api_key`/`base_url` of the resolved instance).

`global_cache` / Parquet keys: none read or written. Note it constructs `TautulliAPI` *without* passing `cache=`, unlike `TautulliInstanceManager`.

dry_run behavior: none — validation is read-only, so dry_run does not alter it.

Singleton / concurrency / threading notes: process-wide singleton via `BaseManager`. Holds no per-instance state (no `self.instances` cache), so each `validate()` call builds a throwaway `TautulliAPI` (and a fresh `requests.Session`); it is effectively stateless and safe to call repeatedly.

## How it functions

Lifecycle: `__init__` simply calls `super().__init__(logger, config, global_cache, **kwargs)` — no extra state, no `load_components`, no `run`. The entire behavior lives in `validate()`, which is invoked on demand. The config-shape discrimination (`all(isinstance(v, str) for v in tautulli_cfg.values())`) mirrors `TautulliInstanceManager`, so flat and multi-instance configs are handled identically.

It delegates **no** decision to any `machine_learning` brain module — reachability is a hard yes/no based on an HTTP response, not a value judgement.

## Criteria & examples

- **Flat config present** — `tautulli = {"url": "10.0.0.5", "port": "8181", "api": "abc123"}`: all values are strings, so `validate("default")` uses the whole block as the instance config and returns `True` iff `get_server_info` responds.
- **Multi-instance, primary present** — `tautulli = {"default": {...}}`: `validate("default")` resolves `tautulli_cfg["default"]` and returns the result of the `get_server_info` round-trip.
- **Optional backup absent** — `tautulli = {"default": {...}}` with no `backup` key: `validate("backup")` finds no config, logs at **info** (`⚠️ Tautulli 'backup' instance not configured — skipping.`), and returns `False` — deliberately quiet because backup is optional.
- **Missing required instance** — `validate("default")` with an empty/absent `tautulli` block: logs at **warning** (`⚠️ Tautulli instance 'default' not found in config.`) and returns `False`.
- **Configured but unreachable** — config exists but the server times out: `api.validate()` returns `None` (after `TautulliAPI`'s internal 3-attempt retry), so `bool(None)` → `False`.

## In plain English

This is the doorbell test. Before the system trusts a Plex-stats branch, it walks up, checks the address is even written down, then rings the bell once and waits to hear someone answer. If the branch is the optional "backup" one and there's no address on file, it just shrugs and moves on quietly. If a branch that's supposed to exist isn't on file, or nobody answers the bell, it reports back "no" so nothing downstream wastes time trying to talk to a branch that isn't there. It makes no judgments about your shows — it only checks whether the line is alive.

## Interactions

- **Parent manager:** auto-linked via `BaseManager`; functionally it backs the parent-level `TautulliValidatorManager` (a separate shim at `tautulli/validator.py`) as the "real validation logic." It is *not* listed in `TautulliManager`'s `all_component_classes`.
- **Helper class it builds (same directory, not a manager):** `TautulliAPI` (`api.py`), whose `validate()` does the `get_server_info` GET.
- **Brain modules:** none — no decision is delegated to `machine_learning/`.
