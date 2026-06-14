# SonarrValidatorKeysManager

**File** — `scripts/managers/services/sonarr/validator/keys.py`
**One-liner** — Manages Sonarr instance credentials: a non-interactive presence check of API keys, an interactive console wizard to (re)configure and live-test instances, a per-instance live reachability probe, and a config-backup export.

## What it does (for a senior Python engineer)

`SonarrValidatorKeysManager(BaseManager, ComponentManagerMixin)` is the `key_validator` component. It sets `self.parent_name = self.__class__.__name__`.

State at init:
- `self.manager` (the `manager` kwarg), dual cache (`self.sonarr_cache`, `self.global_cache`), `self.dry_run` (default `False`).
- Raises `ValueError` if no logger.

Public methods:
- `run_credentials_only()` — The bootstrap-path method (called by `SonarrValidatorManager.audit_bootstrap_instances`). Reads `self.config.get("sonarr_instances", {})` and, per instance, checks whether `cfg.get("api")` is present. Missing keys set registry flag `sonarr.instance.<name>.api_missing = True` and increment `missing`; present keys set `sonarr.instance.<name>.api_present = True` and increment `valid`; exceptions append the name to `errored`. Returns `{"valid": int, "missing": int, "errored": [..], "success": missing == 0 and not errored}`. This is a config-only check — it does NOT make network calls.
- `check_instance_reachability(url, api_key) -> bool` — Live probe. Does `requests.get(f"{url}/api/v3/system/status", headers={"X-Api-Key": api_key}, timeout=5)` and returns `True` iff status `200` and the JSON contains `"version"`. Non-200 or transport errors log a warning and return `False`.
- `prompt_and_repair_instances()` — Interactive console wizard (uses `input()` and `getpass.getpass()`). Prompts for instance count, base URL, starting port; per instance prompts for a name, computes `port = base_port + idx` with optional override, and reads the API key via `getpass`. Builds `{name: {"base_url", "url", "api", "port"}}`. Then live-checks each via `check_instance_reachability`; for failures, optionally re-prompts URL/key; prints a final config preview and, on `yes`, writes `self.config["sonarr_instances"] = sonarr_instances`, logs a reminder to persist, and returns `self.config`. (It mutates the in-memory config but does not itself persist to disk.)
- `backup_all_configs(backup_path)` — `os.makedirs(backup_path, exist_ok=True)`, then for every API in `self.registry.get_all("sonarr_api")` calls `api._make_request(instance, "config")` and, if data, writes `make_json_safe(data)` to `f"{backup_path}/config_{instance}.json"` (indent 2, utf-8). Per-instance failures are logged and skipped.

FETCH / CACHE / APPLY:
- FETCH: `check_instance_reachability` (raw GET to `system/status`) and `backup_all_configs` (via `api._make_request(..., "config")`).
- CACHE: none (no global_cache reads/writes; `backup_all_configs` writes JSON files to a filesystem path, not the cache).
- APPLY: `prompt_and_repair_instances` mutates the in-memory `self.config["sonarr_instances"]` (config write-back, not a Sonarr-side mutation).

External API endpoints: `/api/v3/system/status` (reachability), and the `config` endpoint via `_make_request` (backup).
Config keys read: `sonarr_instances`; per instance, `api` (and in `prompt_and_repair`, the `url`/`base_url`/`port` it writes).
Config keys written: `sonarr_instances` (by `prompt_and_repair_instances`).
Registry flags written: `sonarr.instance.<name>.api_missing`, `sonarr.instance.<name>.api_present`.
Registry read: `get_all("sonarr_api")` (in backup).

dry_run: stored at init but NOT consulted by any method — note that `prompt_and_repair_instances` mutates config and `backup_all_configs` writes files even in dry-run.

Singleton / concurrency: standard `BaseManager` caching. `prompt_and_repair_instances` is blocking/interactive (stdin) and must only run in an attended console context.

## How it functions

Lifecycle: `__init__` (resolve manager/cache/dry_run, register) → callers invoke one of the four methods.

- The automated bootstrap path uses `run_credentials_only()` (pure config inspection, sets registry flags, returns a tally).
- The interactive repair path (`prompt_and_repair_instances`) is a multi-stage wizard: gather → live-validate via `check_instance_reachability` → optional re-entry for failures → preview → confirm → write `self.config`.
- `backup_all_configs` is an export utility independent of the others.

No machine_learning delegation — all logic is deterministic credential plumbing and I/O.

## Criteria & examples

- `run_credentials_only` success criterion: `missing == 0 and not errored`. Example: `sonarr_instances = {"1080": {"api": "abc"}, "4k": {}}` → `1080` flagged `api_present`, `4k` flagged `api_missing` → returns `{"valid": 1, "missing": 1, "errored": [], "success": False}`.
- `check_instance_reachability` true criterion: HTTP 200 AND `"version"` in the JSON. Example: `GET http://localhost:8989/api/v3/system/status` returns 200 with `{"version": "4.0.1.929"}` → `True`. A 200 with `{"error": "Unauthorized"}` (no `version`) → `False`.
- `prompt_and_repair_instances` port defaulting: base port `8989`, three instances `720/1080/4k` → ports `8989/8990/8991` unless overridden; URL `http://localhost` + port `8990` → `http://localhost:8990`.
- `backup_all_configs("/backups")`: for instance `1080`, writes `/backups/config_1080.json` containing the JSON-safe `config` payload; if `_make_request` returns falsy, logs `⚠️ No config data returned from 1080` and writes nothing.

## In plain English

This is the front-desk clerk for your TV servers' keys. In its quiet, automatic mode it just glances at the guest list and notes "this server has its key, that one's missing its key" — no phone calls. There's also a hands-on setup wizard: it walks you through adding servers one by one (name, address, port, secret key), then actually rings each server to confirm the key works, lets you re-type anything that failed, shows you the final list, and saves it once you say "yes." Finally, it can make safety photocopies of each server's settings to a backup folder, so if something gets scrambled you have the originals. Note that the wizard and the backup run for real even in practice mode, so the "just pretend" switch doesn't apply here.

## Interactions

- **Parent manager:** loaded as the critical `key_validator` by `SonarrValidatorManager`, whose `audit_bootstrap_instances` calls `run_credentials_only()`.
- **Siblings:** `SonarrValidatorHealthManager` (the health half of the bootstrap audit), `SonarrValidatorAuthManager`, `SonarrValidatorCacheManager`, `SonarrValidatorFactoryManager`.
- **External:** `requests` (reachability GET), the per-instance Sonarr API clients via the registry (`sonarr_api`) for config backup, `make_json_safe` for serialization, and the on-disk backup path.
- **Brain modules:** none.
