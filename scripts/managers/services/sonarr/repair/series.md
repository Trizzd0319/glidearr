# SonarrRepairSeriesManager

**File** — `scripts/managers/services/sonarr/repair/series.py`
**One-liner** — Audits each Sonarr series for missing/broken paths, invalid root-folder mapping, and unmonitored status — and re-monitors any series found unmonitored.

## What it does (for a senior Python engineer)

`SonarrRepairSeriesManager(BaseManager, ComponentManagerMixin)` is a leaf repair sub-manager under `SonarrRepairManager`. It performs **FETCH** (read series/root folders) and **APPLY** (re-monitor series).

- **Parent:** `self.parent_name = "SonarrRepair"`. Constructed by `SonarrRepairManager` (non-critical).
- **Deps:** `sonarr_api` from the `sonarr_api` kwarg or the `manager` kwarg's attr (raises `ValueError` if unresolved); `manager` from kwarg or `registry.get("manager", parent_name)`; `dry_run` from the manager.
- **Loads submanagers:** none.

Public methods:

- **`check_series_integrity()`** — calls `self.sonarr_api.get_series()` and `self.sonarr_api.get_root_folders()` (note: this calls the API object directly, not a per-instance client from `get_all_sonarr_apis()`), builds the set of root-folder paths, then for each series dict checks: `monitored` flag, presence of `path`, and whether `path` starts with any root folder. **Unmonitored series are re-monitored** via `self.sonarr_api.update_series(s['id'], monitored=True)` (unless dry-run). Missing or invalid paths are logged as errors only (no fix). Returns `None`.
- **`repair_series()`** — convenience entry point that just calls `check_series_integrity()`.

- API endpoints touched: `get_series`, `get_root_folders`, `update_series`.
- Config keys read: none. global_cache keys: none.
- FETCH / CACHE / APPLY: **FETCH + APPLY**.
- dry_run: gates the `update_series` re-monitor call (logged only when dry-run).
- Singleton/threading: standard `BaseManager` singleton; no threading.

## How it functions

Lifecycle: `__init__` sets `parent_name`, calls `super().__init__`, `self.register()`, resolves the API and manager and `dry_run`, raises if no API, logs an init line (including the dry-run state). `repair_series()` simply delegates to `check_series_integrity()`. The integrity scan blends three checks; only the unmonitored case has a corrective action (re-monitor), while path problems are surfaced as errors for a human/another tool. No `machine_learning` brain module is involved.

Implementation note: the re-monitor policy here is unconditional ("if unmonitored → re-monitor"), which differs from the more nuanced watchability-based monitor policies the ML brain uses elsewhere in the project.

## Criteria & examples

- **Unmonitored → re-monitor:** `monitored` is falsy. Example: series "The Wire" has `monitored=False` → logs `⚠️ Unmonitored series: The Wire`, then (unless dry-run) calls `update_series(id, monitored=True)` and logs `✅ Re-monitored series: The Wire`.
- **Missing path:** `path` is falsy → `❌ Missing path for series: <title>` (no fix).
- **Invalid root mapping:** `path` does not start with any root folder path. Example: root folders are `{/data/tv}` but a series path is `/mnt/misc/Show` → `🛑 Invalid root folder for: Show → /mnt/misc/Show` (no fix).

## In plain English

This is the per-show caretaker. It walks down the list of shows and checks three things for each: is the show actually being recorded (monitored), does it have a shelf location at all, and is that location inside one of the official storage areas. If a show somehow got switched to "don't record," the caretaker flips it back on. If a show's shelf location is missing or in the wrong building, it raises a flag for someone to look at but doesn't move it. In practice mode it only announces what it would re-enable.

## Interactions

- **Parent manager:** `SonarrRepairManager`.
- **Siblings:** the other `SonarrRepair*Manager` specialists.
- **Services:** the Sonarr API (`sonarr_api`, called directly).
- **Brain modules:** none (uses a simple unconditional re-monitor rule rather than delegating to a `machine_learning` monitor-policy module).
