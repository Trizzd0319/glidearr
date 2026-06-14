# SonarrRepairInstanceCredentialsManager

- **File** — `scripts/managers/services/sonarr/repair/instance/credentials.py`
- **One-liner** — Audits each Sonarr instance for a usable `api` key, sets present/missing registry flags, and (live mode, full run) marks key-less instances `failed` so they must be fixed before next run.

## What it does (for a senior Python engineer)

Validates that every Sonarr instance has a non-empty `api` key. Two modes: a `full` credential repair and a read-only `bootstrap` audit.

**Construction & tree position.** Subclasses `BaseManager` and `ComponentManagerMixin`. `__init__` sets `self.parent_name = __class__.__name__` (`"SonarrRepairInstanceCredentialsManager"`), stores `self.manager = manager`, and resolves `self.dry_run = kwargs.get("dry_run", getattr(manager, "dry_run", False))` **before** `super().__init__` (same `self.manager`-reassignment caveat documented in the sibling managers). Loads no components. Runtime parent is `SonarrRepairInstanceManager`.

**Public methods.**
- `__init__(... manager=None, **kwargs)` — wiring + early `dry_run` capture; `self.register()`.
- `run(mode="full")` — entry point. If `mode == "bootstrap"` it delegates to `_run_credentials_only()` (audit, never mutates). Otherwise (`full`): iterates `sonarr_instances`, skipping `default_instance` and non-dicts. For each, reads `cfg.get("api")`. If falsy → counts `missing`, and **in live mode only** sets `cfg["failed"] = True` and counts `repaired` (the "repair" is quarantining, not supplying a key — the warning states the key must be added to config before the next run); always sets registry flag `sonarr.instance.<name>.api_missing = True`. If present → counts `valid`, sets `sonarr.instance.<name>.api_present = True`. Per-instance try/except records `errored`. Returns `{"valid", "missing", "repaired", "errored", "success"}` where `success = missing == 0 and len(errored) == 0`.

**Internal helper.**
- `_run_credentials_only()` — the bootstrap path. Same scan/flag logic but **never** mutates config (no `failed` write) even in live mode. Returns `{"valid", "missing", "errored", "success"}`.

**FETCH / CACHE / APPLY.** APPLY only, and minimally — the sole config mutation is `cfg["failed"] = True` in full+live mode. No HTTP (it does not validate the key against Sonarr; it only checks presence). No caching.

**Config keys.** `sonarr_instances`; per-instance `api`; writes per-instance `failed` (full+live only).

**global_cache / Parquet.** None. Writes registry flags: `sonarr.instance.<name>.api_missing` / `sonarr.instance.<name>.api_present` (full mode builds the path as `<flag_path>_missing` / `<flag_path>_present` where `flag_path = sonarr.instance.<name>.api`, yielding the same names).

**dry_run.** When true (full mode), missing keys are still counted and the `api_missing` registry flag is still set, but `cfg["failed"]` is **not** written and `repaired` stays 0. Bootstrap mode never writes config regardless of `dry_run`.

**Singleton / concurrency.** `BaseManager` singleton; single-threaded.

## How it functions

Lifecycle: construct → `run(mode=...)`. `full` walks every instance once, classifying each as valid / missing (and conditionally quarantining) / errored, then logs a summary including the `dry_run` value and total instance count. `bootstrap` is the same classification minus any mutation — intended for an early "do we even have keys?" check during startup. No `machine_learning` delegation.

## Criteria & examples

The guard is purely "is `cfg.get("api")` truthy?" — an empty string, `None`, or missing key all count as missing.

- **Valid:** `{"api": "abc123"}` → `valid += 1`, `sonarr.instance.<name>.api_present` set.
- **Missing, live, full:** `{"api": ""}` → `missing += 1`, `cfg["failed"] = True`, `repaired += 1`, `sonarr.instance.<name>.api_missing` set. (The downstream flag manager would clear that `failed` again next run — a deliberate retry loop.)
- **Missing, dry-run, full:** same as above but `failed` is **not** written and `repaired` stays 0.
- **Bootstrap:** `{"api": ""}` → flagged `api_missing`, counted `missing`, config untouched even live.

Worked example (full, live): instances `main` (`api: "key"`) and `backup` (no `api`) → `{"valid": 1, "missing": 1, "repaired": 1, "errored": [], "success": False}`; `backup` now carries `failed: True`.

## In plain English

Every TV server needs a password (its API key) before Glidearr can talk to it. This step checks each server's wallet: if the password is there, it stamps "good to go"; if it's blank or missing, it puts a "DO NOT USE until you add the password" tag on that server so the app doesn't waste time on it. There's also a quieter "just looking" mode that only reports who has a password and changes nothing. Note this only checks that a key *exists* — it doesn't test whether the key actually works against the server.

## Interactions

- **Parent:** `SonarrRepairInstanceManager` (attribute `self.cred`).
- **Siblings:** flag, reachability, config submanagers (runs third). It *sets* the `failed` flag that the flag manager *clears* — together they form a one-run-grace retry cycle.
- **Brain modules:** none.
- **Other services:** none directly (presence check only, no Sonarr API call).
