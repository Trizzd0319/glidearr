# SonarrRepairValidatorManager

**File** — `scripts/managers/services/sonarr/repair/validator.py`
**One-liner** — Runs read-only sanity checks against a Sonarr instance: validates each series' core fields and confirms the instance's API endpoints respond.

## What it does (for a senior Python engineer)

`SonarrRepairValidatorManager(BaseManager, ComponentManagerMixin)` is a leaf repair sub-manager under `SonarrRepairManager`. It performs **FETCH-only** validation — it reads from Sonarr and logs problems but never writes back.

- **Parent:** declares `self.parent_name = "SonarrRepair"`. Constructed by `SonarrRepairManager` and listed in its `critical_keys`.
- **Required deps:** `sonarr_api` and `instance_manager` — both must be supplied or the constructor raises `ValueError("Missing required API or instance manager…")`. Also accepts `sonarr_cache` and `dry_run` (stored, but neither is used by the two methods).
- **Loads submanagers:** none.

Public methods:

- **`validate_series_integrity(instance_name)`** — resolves the instance via `instance_manager.resolve_instance(instance_name)`, gets the per-instance API client from `sonarr_api.get_all_sonarr_apis()[resolved_instance]`, calls `api.all_series()`, and for each series checks: missing `id`/`title`, missing `path`, and missing/empty `seasons`. It logs a warning enumerating the issues per series, or a debug "validated" line if clean. Returns `None`.
- **`validate_endpoint_health(instance_name)`** — resolves the instance, then calls three Sonarr endpoints — `api.get_system_status()`, `api.get_disk_space()`, `api.get_tags()` — and warns if the status is empty, the disk-space response is not a non-empty list, or the tag list is not a list. Logs an info line on success; catches and logs any exception. Returns `None`.

- API endpoints touched (via the client): `all_series`, `get_system_status`, `get_disk_space`, `get_tags`.
- Config keys read: none. global_cache keys: none.
- FETCH / CACHE / APPLY: **FETCH only**; no CACHE, no APPLY.
- dry_run: stored on `self.dry_run` but not consulted (it never writes, so it is inherently safe).
- Singleton/threading: standard `BaseManager` singleton; no threading.

## How it functions

Lifecycle: `__init__` sets `parent_name = "SonarrRepair"`, calls `super().__init__`, `self.register()`, captures the injected deps, validates that API + instance manager are present (else raises), and logs a debug init line. Both public methods follow the same pattern: resolve instance → fetch via the per-instance API client → inspect fields → log. No `machine_learning` brain module is involved.

## Criteria & examples

- **Series integrity guards:** a series fails if `not series.title or not series.id`, `not series.path`, or `not series.seasons or not any(series.seasons)`. Example: a series titled "Firefly" with a valid id but `path = ""` logs `⚠️ Validation issue for 'Firefly' (ID 14): 📂 Missing path`.
- **Endpoint health guards:** disk space must be a non-empty `list` and tags must be a `list`. Example: if `get_disk_space()` returns `[]`, it logs `⚠️ Disk space response empty for instance: <name>` but still completes the check and logs the success line.

## In plain English

This is the shop's quality inspector with a clipboard. It walks the shelf of TV shows and checks each one has a name, an ID tag, a place on the shelf, and at least one season listed; anything missing gets a note. Then it knocks on the back-office door (the Sonarr server) to confirm it answers, has disk space to report, and can list its labels. The inspector never moves or changes anything — it only writes down what looks wrong so a human (or another specialist) can act on it later.

## Interactions

- **Parent manager:** `SonarrRepairManager`.
- **Siblings:** the other `SonarrRepair*Manager` specialists.
- **Services:** the Sonarr per-instance API client (via `sonarr_api`) and the instance resolver (`instance_manager`).
- **Brain modules:** none.
