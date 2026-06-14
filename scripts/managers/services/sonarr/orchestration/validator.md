# SonarrOrchestrationValidatorManager

**File** — `scripts/managers/services/sonarr/orchestration/validator.py`
**One-liner** — Orchestration façade over the Sonarr *validator* submanagers: full validation sweeps (credentials, health, auth, cache), bootstrap audits, credential repair/backup, and registry-flag summaries.

## What it does (for a senior Python engineer)

`SonarrOrchestrationValidatorManager(BaseManager, ComponentManagerMixin)`. `parent_name` is derived from the class name. Resolves the dual cache, `manager`, `dry_run`, and then — importantly — `self.validator = manager.validator_manager` (the `SonarrValidatorManager`), **not** `manager.validator` (which is the BaseManager factory validator). This naming distinction is called out in a code comment.

**Self-disabling:** if `manager.validator_manager` is unavailable, sets `self.active = False`, `self._inactive_reason = "SonarrValidatorManager (validator_manager) unavailable — validator orchestration disabled."`, and returns **before** `self.register()`. Otherwise `self.active = True` and it registers.

Key public methods:
- `run_full_validation()` — returns a results dict with four phases: `credentials` (`validator.key_validator.run_credentials_only()`), `health` (`validator.health_validator.verify_all_instances_health()`), `auth` (`validator.auth_handler.run_auth_diagnostics()` if that method exists, else `"Not implemented"`), `cache` (`validator.cache_manager.audit_cache_index()` if it exists, else `"Not implemented"`).
- `run_bootstrap_audit()` → `validator.audit_bootstrap_instances()` (API-key presence + health check).
- `refresh_credentials()` → `validator.key_validator.prompt_and_repair_instances()` (interactive reconfigure).
- `export_all_configs(backup_path="./exports/sonarr")` → `validator.key_validator.backup_all_configs(backup_path)`.
- `summarize_validation()` — iterates `config.get("sonarr_instances", {})` and builds a per-instance status dict from registry flags (see below).

FETCH / APPLY: mostly read/diagnostic (FETCH-shaped health and credential checks); `refresh_credentials` and `export_all_configs` are APPLY-shaped (mutate/persist via the key_validator leaf). No cache writes of its own.

External API endpoints: none directly; the health/key validators issue Sonarr `system_status`-style checks.

Config keys read: `sonarr_instances` (enumerated by `summarize_validation`).

Registry flags read (per instance `name` in `summarize_validation`):
- `sonarr.instance.{name}.api_present`
- `sonarr.instance.{name}.api_missing`
- `sonarr.instance.{name}.health_ok`
- `sonarr.instance.{name}.health_fail`

global_cache / Parquet keys: none directly (the `cache_manager` leaf audits the cache index when present).

dry_run: `self.dry_run = manager.dry_run` (read-only here, no gating).

Concurrency: none.

## How it functions

Lifecycle: `__init__` resolves `self.validator` (the `validator_manager`), short-circuits to inactive if absent (so the parent skips it), else registers. `run_full_validation` uses `hasattr` guards on the `auth`/`cache` phases so optional validator capabilities degrade to `"Not implemented"` rather than raising. `summarize_validation` is purely a registry-flag reporter keyed off the configured instance list.

Brain delegation: none.

## Criteria & examples

The decision logic is the `hasattr` capability gating and the registry-flag readout. Worked example: for instance `default`, `summarize_validation()` returns `{"default": {"api_present": True, "api_missing": False, "health_passed": True, "health_failed": False}}` when the key/health validators have set those flags — a clean instance. If `auth_handler` lacks `run_auth_diagnostics`, `run_full_validation()["auth"]` is the string `"Not implemented"` instead of crashing.

## In plain English

This is the security-and-health checkpoint for your Sonarr servers. It confirms each server's API key is present and valid, pings them to make sure they're alive, and can back up their configs or walk you through fixing a broken credential. The summary view is like a dashboard of green/red lights per server: "key present? yes; healthy? yes." If a particular check isn't built yet, it politely reports "not implemented" rather than throwing a wrench in the works. The point: you find out a server is misconfigured or unreachable before it silently fails to grab your shows.

## Interactions

- **Parent manager:** `SonarrManager` (resolved as `manager`); constructed by `SonarrOrchestrationManager` as its `validator` child (honours the `active` flag).
- **Leaf submanagers driven:** `validator_manager`'s `key_validator`, `health_validator`, `auth_handler`, `cache_manager`, plus the validator's own `audit_bootstrap_instances`.
- **Brain modules:** none.
