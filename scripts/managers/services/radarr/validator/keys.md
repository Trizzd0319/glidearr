# RadarrValidatorKeysManager

- **File** — `scripts/managers/services/radarr/validator/keys.py`
- **One-liner** — Live-validates Radarr API keys by hitting each instance over HTTP, backs up every instance's server config to disk, and (for direct script use) runs an interactive CLI wizard to re-enter instance settings.

## What it does (for a senior Python engineer)

`RadarrValidatorKeysManager(BaseManager, ComponentManagerMixin)` is the "heaviest" validation leaf under `RadarrValidatorManager`. Unlike the auth submanager (which only checks config presence), this one actually proves credentials work against the network, persists config backups to the filesystem, and offers an interactive repair flow.

Position in the tree: `parent_name = "RadarrValidatorManager"`. No submanagers loaded. `BaseManager` singleton with the usual injected deps plus `self.radarr_api`, `self.instance_manager`, `self.dry_run` resolved in `__init__`.

- **FETCH**: yes — both a direct `requests.get` to `system/status` and `_make_request` calls for backups. **CACHE**: none (writes to disk, not `global_cache`). **APPLY**: only the interactive wizard mutates `self.config` in memory (and even then leaves persistence to the caller).
- **Public methods**:
  - `check_instance_reachability(url: str, api_key: str) -> bool` — raw HTTP `GET {url}/api/v3/system/status` with header `X-Api-Key: <api_key>`, `timeout=5`; returns `True` iff status 200 **and** the JSON body contains `"version"`. Logs a warning on non-200 or on connection error and returns `False`.
  - `validate_all_keys() -> dict` — for each `config["radarr_instances"]` entry, reads `url` (or `base_url`) and `api` (or `api_key`); if either is missing it logs a warning and records `False`, otherwise calls `check_instance_reachability`. Returns `{instance: bool}`.
  - `backup_all_configs(backup_path: str)` — `os.makedirs(backup_path, exist_ok=True)`, then for each instance from `_get_all_apis()` fetches the four endpoints `config/host`, `config/naming`, `config/mediamanagement`, `qualityprofile` via that instance's `api._make_request`, aggregates them under `endpoint.replace("/", "_")` keys, and writes `config_<instance>.json` (pretty-printed, `make_json_safe`-sanitized) per instance. Skips instances that returned no data.
  - `prompt_and_repair_instances()` — interactive CLI wizard (uses `input()` and `getpass.getpass()`); intended only for direct script invocation, not automated runs. Prompts for instance count, base URL, starting port, per-instance name/port/API key; reachability-checks each; offers to re-enter failed ones; on confirmation writes the new map to `self.config["radarr_instances"]` and returns `self.config` (persistence is left to the caller — it logs "Persist the updated config to disk").
- **Internal helpers**:
  - `_get_all_apis() -> dict` — `{instance_name: api}` from `radarr_api.get_all_radarr_apis()`, fallback `instance_manager.get_all_radarr_apis()`, else `{}` (guarded). (Note this returns the api objects, whereas the sibling managers' `_get_all_instances` returns just the name keys.)
- **External API endpoints**: `system/status` (direct `requests`, v3); `config/host`, `config/naming`, `config/mediamanagement`, `qualityprofile` (via `api._make_request` during backup).
- **Config keys read**: `radarr_instances` (and per-instance `url`/`base_url`, `api`/`api_key`).
- **Config keys written**: `radarr_instances` — only inside `prompt_and_repair_instances`, in memory.
- **global_cache / Parquet keys**: none. Disk artifacts: `config_<instance>.json` files under `backup_path`.
- **dry_run**: captured but **not honored** — `backup_all_configs` writes files and `prompt_and_repair_instances` mutates `self.config` regardless of `self.dry_run`. (Neither performs a remote PUT/DELETE, so no Radarr state is changed; the wizard is human-gated and interactive.)
- **Singleton / concurrency**: `BaseManager` singleton; sequential I/O, no threading. `prompt_and_repair_instances` blocks on stdin and must not be called from automated flows.

## How it functions

Lifecycle: `__init__` → `super().__init__` → `register()` → resolve deps → debug log. No `load_components`. The three public surfaces are independent entry points:
- automated validation → `validate_all_keys()` (loops config, delegates to `check_instance_reachability`);
- maintenance/backup → `backup_all_configs(path)` (loops `_get_all_apis()`, fetches the four config endpoints, writes JSON);
- manual repair → `prompt_and_repair_instances()` (CLI wizard).

No `machine_learning` brain module is involved — reachability is a deterministic 200-plus-`version` check, and repair is purely operator-driven.

## Criteria & examples

- **Reachable** iff HTTP 200 **and** `"version"` present in the JSON body. A 200 without `version`, any non-200, or a connection error all → `False`.
- Example: `check_instance_reachability("http://localhost:7878", "abc123")` → server returns 200 with `{"version": "5.2.6", ...}` → `True`. Same call when Radarr is down → connection error caught → logs "Connection error to http://localhost:7878: ..." → `False`.
- Example (`validate_all_keys`): `radarr_instances = {"1080": {"url": "http://localhost:7878", "api": "k"}, "4k": {"api": "k"}}` → `"4k"` has no `url`/`base_url`, so it short-circuits to `False` with a "Missing URL or API key" warning; `"1080"` gets a live reachability check. Result e.g. `{"1080": True, "4k": False}`.
- Example (`backup_all_configs("/backups")`): for instance `1080`, fetching the four endpoints yields data for three of them → writes `/backups/config_1080.json` containing keys `config_host`, `config_naming`, `qualityprofile` (the `config/mediamanagement` fetch returned nothing and is omitted). An instance whose four fetches all return empty is skipped with "No config data fetched".

## In plain English

This is the inspector who doesn't just check the guest list — they walk up to each Radarr server's door, try the key in the lock, and confirm a real person answers with their ID ("version 5.2.6, come on in"). It also keeps a photocopy of each server's important paperwork (its naming rules, quality profiles, and storage settings) in a backup folder, so if something gets misconfigured later you have the originals. And if you're running it by hand, it can walk you through setting up your servers step by step — like a friendly setup wizard that asks "how many libraries, what's the address, what's the password?" and tests each one before saving.

## Interactions

- **Parent manager**: `RadarrValidatorManager`.
- **Sibling submanagers**: `RadarrValidatorAuthManager` (config-only key presence), `RadarrValidatorCacheManager`, `RadarrValidatorHealthManager` (which does the same reachability idea but through `_make_request`).
- **Other services**: the shared `radarr_api` client / `instance_manager` (`get_all_radarr_apis`, `_make_request`), `ConfigManager` (`self.config`), `requests` (direct HTTP), and `make_json_safe` (from `scripts/managers/factories/cache.py`) for backup serialization.
- **Brain modules**: none.
