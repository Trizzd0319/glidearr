# SonarrOrchestrationInstanceManager

**File** — `scripts/managers/services/sonarr/orchestration/instance.py`
**One-liner** — Instance-level diagnostics/orchestration: runs health, version, disk, and queue summaries across all configured Sonarr instances.

## What it does (for a senior Python engineer)

`SonarrOrchestrationInstanceManager(BaseManager, ComponentManagerMixin)`. `parent_name` is derived from the class name. It resolves the dual cache, `logger`, `sonarr_api`, `manager`, `dry_run`, and crucially `self.instance_manager = manager.instance_manager` (the per-instance API registry). Raises `ValueError` if it has no logger. Loads no submanagers.

Key public methods:
- `run_instance_diagnostics()` — for every API in `instance_manager.get_all_sonarr_apis()`, calls `api.system_status()`, `api.disk_space()`, `api.queue()` and builds a per-instance summary dict `{version, disk_free_gb, queue_size}`. `disk_free_gb` is `sum(d.freeSpace / 1e9 for d in disk_space if hasattr(d, "freeSpace"))`. Per-instance failures are caught and recorded as `{"error": str(e)}`. Returns the summaries dict.
- `validate_all_instances()` — for each name in `instance_manager.get_all_instance_names()`, fetches the API and calls `api.system_status()`; records `"✅ OK"` or `"❌ Failed: <e>"`. Returns the results dict.
- `summarize_all_instances()` — returns `{name: instance_manager.summarize_instance(name)}` for all instance names.

FETCH: yes — read-only HTTP GETs (`system_status`, `disk_space`, `queue`). CACHE/APPLY: none.

External API endpoints (Sonarr, via the api object): `system_status()`, `disk_space()`, `queue()`.

Config keys: none read directly (instance enumeration goes through `instance_manager`).

global_cache / Parquet keys: none read/written.

dry_run: captured but irrelevant — all calls here are read-only.

Concurrency: none (sequential per-instance loops).

## How it functions

Lifecycle: `__init__` wires deps (notably `instance_manager`), no `run()` entry method. Each public method early-returns with a logged warning/error if `instance_manager` is missing. All three methods are defensively wrapped so one bad instance does not abort the sweep over the others.

Brain delegation: none.

## Criteria & examples

No selection thresholds; it is reporting only. Worked example: with two instances `default` and `anime`, `run_instance_diagnostics()` queries both. If `anime` is unreachable, its `system_status()` raises, the exception is caught, and the result is `{"anime": {"error": "..."}}` while `default` still returns `{"version": "4.0.x", "disk_free_gb": 213.4, "queue_size": 2}`. The `disk_free_gb` sums every disk's `freeSpace` bytes divided by `1e9` (so reported in GB, decimal).

## In plain English

This is the clipboard-and-stethoscope routine for your Sonarr servers. It walks up to each Sonarr instance you've configured, asks "what version are you, how much free disk do you have, and how big is your download queue?", and writes it all down. If one server is down, it just notes "this one errored" and keeps checking the rest. Think of it as a quick morning health check of every TV-download machine you run, so you can see at a glance which one is low on space or stuck.

## Interactions

- **Parent manager:** `SonarrManager` (resolved as `manager`); constructed by `SonarrOrchestrationManager` as its `instance` child.
- **Collaborator:** `SonarrManager.instance_manager` (the `SonarrInstanceManager`, the canonical `sonarr_api` reference per project conventions) for enumerating instances and per-instance API handles.
- **Submanagers:** none.
- **Brain modules:** none.
