# SonarrRepairMonitoringManager

**File** — `scripts/managers/services/sonarr/repair/monitoring.py`
**One-liner** — Corrects Sonarr series monitored flags so that ended series are unmonitored and ongoing series are monitored, persisting changes via the API.

## What it does (for a senior Python engineer)

`SonarrRepairMonitoringManager(BaseManager, ComponentManagerMixin)` is a leaf repair sub-manager under `SonarrRepairManager`. It performs **APPLY** (update monitoring flags) on a supplied list of series; it does not FETCH the list itself (the caller passes it in).

- **Parent:** `self.parent_name = "SonarrRepair"`. Constructed by `SonarrRepairManager` and listed in its `critical_keys`.
- **Deps:** `sonarr_api` from the `sonarr_api` kwarg or the registered/passed manager; `dry_run` from kwarg or manager. No hard `ValueError` precondition (API absence is tolerated and simply skips the persist step).
- **Loads submanagers:** none.

Public method:

- **`repair_monitoring_flags(series_list)`** — iterates the provided list of series dicts (skips non-dicts). For each it derives `should_be_monitored = "ended" not in status.lower()`. If the current `monitored` value is `None` or differs from `should_be_monitored`, it sets `series["monitored"] = should_be_monitored` in the dict and, when `sonarr_api` and `series["id"]` are present, persists via `self.sonarr_api.update_series_monitoring(series["id"], monitored=should_be_monitored)` (unless dry-run). Appends the title to `repaired`. Returns the list of repaired titles.

- API endpoints touched: `update_series_monitoring`.
- Config keys read: none. global_cache keys: none.
- FETCH / CACHE / APPLY: **APPLY** only (caller supplies the data).
- dry_run: when true, logs `[DRY-RUN] Would set monitored=… via API` and skips the persist call (but still mutates the in-memory dict and counts it as repaired).
- Singleton/threading: standard `BaseManager` singleton; no threading.

## How it functions

Lifecycle: `__init__` sets `parent_name`, calls `super().__init__`, `self.register()`, resolves the API + `dry_run`, logs an init line. The single public method computes the correct monitored state purely from the `status` string (anything containing "ended" → should be unmonitored) and reconciles each series whose flag is wrong or unset. Persistence is per-series in a `try/except`. No `machine_learning` brain module is involved — the rule is a simple status-string heuristic.

## Criteria & examples

- **Should-be-monitored rule:** `"ended" not in status.lower()`. Example: a series with `status="continuing"` → `should_be_monitored=True`. A series with `status="ended"` → `should_be_monitored=False`.
- **Reconcile trigger:** `monitored is None or monitored != should_be_monitored`. Example: "The Office" has `status="ended"` but `monitored=True` → mismatch → flag set to `False`, and (unless dry-run) `update_series_monitoring(id, monitored=False)` is called. A continuing series already `monitored=True` is left untouched.

## In plain English

This specialist decides which shows the recorder should keep watching for new episodes. The rule is simple: if a show has finished its run for good ("ended"), stop monitoring it — no new episodes will ever come. If it's still going, keep monitoring it so new episodes get grabbed. It goes through the list and flips any show that's set the wrong way, then tells Sonarr to remember the change. In practice mode it just announces what it would flip.

## Interactions

- **Parent manager:** `SonarrRepairManager`.
- **Siblings:** the other `SonarrRepair*Manager` specialists (a caller typically supplies the series list it gathered from Sonarr).
- **Services:** the Sonarr API (`sonarr_api`).
- **Brain modules:** none (uses a status-string heuristic rather than a `machine_learning` monitor-policy decision).
