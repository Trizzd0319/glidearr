# SonarrMonitoringSpaceThresholdsManager

- **File** — `scripts/managers/services/sonarr/monitoring/space_thresholds.py`
- **One-liner** — Classifies each Sonarr instance's free-disk-space percentage into `critical` / `warning` / `ok` severities and caches the result, so downstream monitoring can react to storage pressure.

## What it does (for a senior Python engineer)

`SonarrMonitoringSpaceThresholdsManager(BaseManager, ComponentManagerMixin)` is a submanager under `SonarrMonitoring` (declared `parent_name = "SonarrMonitoring"`). It resolves its parent from the `manager` kwarg or the registry, and pulls `sonarr_api`, `sonarr_cache`, and `dry_run` from that parent.

It is primarily **CACHE** (read + write of the cache) — it FETCHes nothing over HTTP itself; it reads already-cached free-space percentages and writes severity verdicts back. It loads no submanagers via `load_components`.

Public methods:
- `get_thresholds_for_instance(instance) -> dict` — returns `config["sonarr_thresholds"][instance]`, defaulting to `{"critical": 5, "warning": 10}` when the instance isn't configured.
- `classify_threshold(percent_free, instance=None) -> str` — returns `"critical"` if `percent_free < critical`, else `"warning"` if `percent_free < warning`, else `"ok"`. Uses the `"default"` instance thresholds when `instance` is `None`.
- `evaluate_all_instance_thresholds() -> dict` — iterates `sonarr_api.get_all_sonarr_apis()`, reads `sonarr_cache["sonarr/<instance>/storage/free_percent"]` (defaulting to `100` if absent), classifies it, and returns `{instance: {percentFree, severity, timestamp(UTC ISO), instance}}`.
- `store_threshold_evaluations(evaluations)` — for each instance, builds a cache key via `sonarr_cache.format_cache_key("sonarr/storage/thresholds", instance=instance)` and `set`s the evaluation dict.
- `run_threshold_audit() -> dict` — the entry point: `evaluate_all_instance_thresholds()` then `store_threshold_evaluations(...)`, then logs and returns the results.
- `get_critical_instances()` / `get_warning_instances()` / `get_ok_instances()` — re-evaluate and filter instance names by severity.
- `has_critical_thresholds()` / `has_warning_thresholds()` — boolean convenience wrappers.
- `get_all_evaluation_data()` — alias for `evaluate_all_instance_thresholds()`.
- `summarize_status() -> dict` — counts of `critical`/`warning`/`ok` instances; logs the summary.

**Config keys read:** `sonarr_thresholds` (a dict keyed by instance name, each with `critical` / `warning` numeric percentages).
**Cache keys read:** `sonarr/<instance>/storage/free_percent`.
**Cache keys written:** `sonarr/storage/thresholds` formatted per instance (via `format_cache_key`).
**API touched:** `sonarr_api.get_all_sonarr_apis()` (instance enumeration only — no per-disk HTTP call here).
**dry_run:** captured into `self.dry_run` but **not consulted** — this manager only reads/writes the cache (no mutating Sonarr API calls), so dry_run has no effect on its behavior.

## How it functions

Lifecycle: `__init__` → `super().__init__` → `register()` → resolve parent/api/cache/dry_run → debug log. There is no `load_components` call. The natural control flow is `run_threshold_audit()` → evaluate → store. The getter helpers (`get_critical_instances`, `summarize_status`, etc.) each independently re-run `evaluate_all_instance_thresholds()`, so calling several of them in succession re-reads the cache multiple times (no memoization). No `machine_learning` brain module is invoked — the classification is a plain numeric comparison in this file.

## Criteria & examples

Severity rule per instance (defaults `critical=5`, `warning=10`):
- A Sonarr instance reporting `free_percent = 3.0` → `3.0 < 5` → **critical**.
- An instance at `free_percent = 8.5` → not `< 5` but `8.5 < 10` → **warning**.
- An instance at `free_percent = 42.0` → neither → **ok**.
- An instance with no cached free-percent → defaults to `100` → **ok**.

If `config["sonarr_thresholds"]["anime4k"] = {"critical": 8, "warning": 15}`, then that instance at `free_percent = 12` → `12 < 15` (not `< 8`) → **warning**, even though the same 12% would be **ok** under the default thresholds.

## In plain English

This is the storage "fuel gauge" for your TV download drives. For each drive it asks "how empty is it?" and lights one of three lamps: green (ok), yellow (getting full — warning), or red (almost full — critical). It writes that reading down so the rest of the system knows whether it's safe to keep adding new episodes of, say, *The Mandalorian*, or whether it's time to start clearing space. The exact red/yellow lines can be set per drive, but if you don't set them, red means under 5% free and yellow means under 10%.

## Interactions

- **Parent:** `SonarrMonitoring` (`SonarrMonitoringManager`).
- **Siblings:** the other seven monitoring submanagers; `SonarrMonitoringPriorityQueueManager` independently re-implements the same `critical<5 / warning<10` `classify_severity` logic and also reads `sonarr/<instance>/storage/free_percent`.
- **Services:** the Sonarr API client (for instance enumeration) and the Sonarr cache manager (for free-space reads and threshold writes).
- **Brain modules:** none.
