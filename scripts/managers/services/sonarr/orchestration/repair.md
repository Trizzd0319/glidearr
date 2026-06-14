# SonarrOrchestrationRepairManager

**File** — `scripts/managers/services/sonarr/orchestration/repair.py`
**One-liner** — Orchestration façade over the Sonarr *repair* submanagers: a flat set of one-line delegations plus a single `run_all_repairs()` that sweeps every repair routine in order.

## What it does (for a senior Python engineer)

`SonarrOrchestrationRepairManager(BaseManager, ComponentManagerMixin)` with `parent_name = "SonarrManager"`. Resolves `manager` (from the `manager` kwarg or `registry.get("manager", "SonarrManager")`) and `self.repair = manager.repair`.

**Self-disabling:** if `manager.repair` is unavailable, sets `self.active = False`, `self._inactive_reason = "SonarrRepairManager unavailable — repair orchestration disabled."`, and returns. Otherwise `self.active = True`.

Public methods (each a terse delegation, several single-line with multiple statements):
- `run_instance_repairs(**kwargs)` → `repair.instance.run(**kwargs)`
- `run_metadata_repairs(series_list)` → `repair.metadata.repair_missing_metadata(series_list)`
- `run_filepaths_repairs()` → `repair.filepaths.repair_root_folder_mappings()`, `cleanup_orphaned_folders()`, `purge_orphaned_cache_keys()`
- `run_storage_repairs()` → `repair.storage.run()`
- `run_quality_repairs()` → `repair.quality.repair_quality_definitions()`
- `run_validator_checks(instance)` → `repair.validator.validate_series_integrity(instance)`, `validate_endpoint_health(instance)`
- `run_tag_repairs()` → `repair.tags.repair_ghost_tags()`, `deduplicate_tags()`
- `run_series_repairs()` → `repair.series.validate_series_fields()`, `flag_unmonitored_series()`
- `run_orphan_repairs()` → `repair.orphans.remove_orphaned_series()`, `cleanup_tagless_metadata()`
- `run_file_repairs()` → `repair.file.validate_episode_files()`
- `run_cache_repairs()` → `repair.repair_cache.purge_invalid_keys()`, `refresh_all_entries()`
- `run_anomaly_repairs()` → `repair.anomaly.detect_unexpected_entries()`
- `run_monitoring_repairs()` → `repair.monitoring.sync_monitoring_flags()`
- `run_history_repairs()` → `repair.history.repair_missing_history()`
- `run_episodes_repairs()` → `repair.episodes.validate_episode_entries()`, `fix_episode_status()`
- `run_all_repairs(series_list=None, instance_name=None)` — the aggregate; see ordering below.

FETCH / CACHE / APPLY: heavily APPLY-shaped — repairs mutate folder mappings, tags, monitoring flags, quality definitions, cache entries, and episode status via the leaf repair submanagers. Some routines also read/validate (FETCH).

External API endpoints: none directly (the repair leaves issue Sonarr calls).

Config keys: none read directly.

global_cache / Parquet keys: indirectly — `run_cache_repairs` and `purge_orphaned_cache_keys` manipulate cache keys through the repair leaves.

dry_run: not captured or threaded in this file; the leaf repair submanagers are responsible for honouring dry_run (consistent with the project's dry_run-propagation footgun — verify at the leaf level).

Concurrency: none here (sequential sweep).

## How it functions

Lifecycle: `__init__` resolves `self.repair` and the soft-disable `active` flag. `run_all_repairs` is the only aggregate; it runs, in order: instance → (optional) validator checks if `instance_name` given → filepaths → storage → quality → series → tags → orphans → file → cache → anomaly → monitoring → history → episodes → (optional) metadata if `series_list` given. Logs a start and end banner.

Brain delegation: none.

## Criteria & examples

The two conditional branches are the only "rules": `run_validator_checks(instance_name)` runs only when `instance_name` is truthy, and `run_metadata_repairs(series_list)` runs only when `series_list` is provided. Worked example: `run_all_repairs(instance_name="default")` will additionally validate series integrity and endpoint health for `default`; `run_all_repairs()` with no args skips both the validator checks and the metadata repair, running the 13 unconditional repair groups.

## In plain English

This is the maintenance crew that fixes the little things that drift out of place over time. On a full pass it: re-points folders that moved, dusts off duplicate or ghost tags, removes shows that no longer exist, double-checks episode files, refreshes stale cached data, and flags weird leftover entries. Think of it like a building superintendent doing a walkthrough — tightening loose screws, replacing burnt-out bulbs, and tossing junk left in the hallways — so the rest of the system runs on a tidy, consistent library instead of accumulating cruft.

## Interactions

- **Parent manager:** `SonarrManager` (resolved as `manager`); constructed by `SonarrOrchestrationManager` as its `repair` child (honours the `active` flag).
- **Leaf submanagers driven:** `repair.instance`, `repair.metadata`, `repair.filepaths`, `repair.storage`, `repair.quality`, `repair.validator`, `repair.tags`, `repair.series`, `repair.orphans`, `repair.file`, `repair.repair_cache`, `repair.anomaly`, `repair.monitoring`, `repair.history`, `repair.episodes`.
- **Brain modules:** none.
