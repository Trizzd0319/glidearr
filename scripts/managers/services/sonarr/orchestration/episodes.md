# SonarrOrchestrationEpisodesManager

**File** — `scripts/managers/services/sonarr/orchestration/episodes.py`
**One-liner** — Orchestration façade over the Sonarr *episodes* submanagers: warmups, recent-episode checks, missing-file/orphan audits, sharding plans, enrichment, deletion audits, and per-episode monitoring toggles.

## What it does (for a senior Python engineer)

`SonarrOrchestrationEpisodesManager(BaseManager, ComponentManagerMixin)` with `parent_name = "SonarrOrchestration"`. It resolves `manager` (the top-level `SonarrManager`), `sonarr_api`, `sonarr_cache`, `dry_run`, then grabs the episode submodules off `manager.episodes`:
- `self.retrieval` = `episodes.retrieval`
- `self.history` = `episodes.history`
- `self.monitoring` = `episodes.monitoring`
- `self.file` = `episodes.file`
- `self.sharding` = `episodes.sharding`
- `self.deletion` = `episodes.deletion`

It tolerates missing submodules (no hard `ValueError` here; the methods assume the relevant submodule exists when called). Loads no submanagers of its own.

Key public methods:
- `run_full_episode_retrieval()` — full warmup: `self.retrieval.episode_cache.warm_all_episodes_cache()`.
- `run_recent_episode_check(instance, hours=24)` — `self.retrieval.fetch.get_recent_episode_ids(instance, hours=hours)`; returns the recent IDs.
- `run_missing_file_audit(instance)` — loads cached episodes via `retrieval.fetch._get_cached_episodes_by_instance(instance)`, then `retrieval.validate.identify_missing_episode_files(...)`; returns the missing list.
- `run_episode_shard_plan(shard_size=10)` — `self.sharding.generate_global_shard_plan(shard_size=shard_size)`; returns the plan.
- `run_orphaned_file_check(instance)` — `self.file.find_orphaned_episode_files(instance)`.
- `run_episode_enrichment(instance)` — `self.retrieval.enrich.run_tvdb_crosslink(instance)`.
- `run_episode_deletion_audit(instance)` — `self.deletion.audit_mismatched_resolutions(instance)`.
- `run_monitoring_toggle(instance, episode_id, enable)` — `self.monitoring.toggle_episode_monitoring(instance, episode_id, enable)` (the one APPLY-shaped method).

FETCH / CACHE / APPLY: drives FETCH (recent IDs, episode fetch), CACHE (warm_all_episodes_cache), and a single APPLY (`toggle_episode_monitoring`, a Sonarr monitoring PUT). The audits are read-only.

External API endpoints: none called directly; the leaf `fetch`/`monitoring`/`deletion` submodules issue Sonarr calls (episode list, history-based recent fetch, monitoring toggle, etc.).

Config keys: none read directly.

global_cache / Parquet keys: indirectly — `warm_all_episodes_cache` and `_get_cached_episodes_by_instance` read/write the episode cache through the retrieval leaf.

dry_run: captured from kwargs/parent; the only mutating path (`run_monitoring_toggle`) defers dry_run handling to the `monitoring` leaf.

Concurrency: none here (the cache child handles parallelism).

## How it functions

Lifecycle: `__init__` wires the six submodule handles, then each public method is a one-line delegation to the relevant episode submanager, decorated with `@timeit` and the logger entry decorator. There is no aggregate `run()`; callers invoke individual orchestration verbs.

Brain delegation: none directly. (Deletion *decisions* and resolution-mismatch judgements are made in the `deletion`/`file` leaves and, ultimately, the broader space/lifecycle brain — not in this file.)

## Criteria & examples

The only parameterised behaviour is the lookback window and shard size. Worked example: `run_recent_episode_check("default", hours=24)` asks the fetch leaf for episodes Sonarr's history touched in the last 24 hours and logs e.g. `🕒 Found 7 recent episodes in past 24 hours.` `run_episode_shard_plan(shard_size=10)` buckets the library into batches of 10 series per shard so large libraries can be processed in chunks rather than one giant pass.

## In plain English

This is the "episode logistics" desk. It can pre-load every episode's details so things are fast later; spot episodes that went missing or have leftover junk files; chunk a huge library into manageable batches; and flip a single episode's "keep watching for this" switch on or off. For example, if you just finished season 1 of The Mandalorian, the monitoring toggle could stop chasing files for episodes you already have. Most of these buttons just look and report; only the monitoring toggle actually changes a setting.

## Interactions

- **Parent manager:** `SonarrManager` (resolved as `manager`); constructed by `SonarrOrchestrationManager` as its `episodes` child.
- **Leaf submodules driven:** `episodes.retrieval` (→ `episode_cache`, `fetch`, `validate`, `enrich`), `episodes.sharding`, `episodes.file`, `episodes.deletion`, `episodes.monitoring`; (`episodes.history` is held but not used by the current methods).
- **Brain modules:** none directly.
