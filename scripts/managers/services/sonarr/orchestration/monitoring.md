# SonarrOrchestrationMonitoringManager

**File** — `scripts/managers/services/sonarr/orchestration/monitoring.py`
**One-liner** — Orchestration façade over the Sonarr *monitoring* submanagers: space-threshold audits, rule sync, backfill, episode/series monitoring updates, priority scheduling, and an end-to-end pipeline.

## What it does (for a senior Python engineer)

`SonarrOrchestrationMonitoringManager(BaseManager, ComponentManagerMixin)` with `parent_name = "SonarrOrchestration"`. Resolves `manager` (top-level `SonarrManager`), `sonarr_cache`, `dry_run`, then `self.monitoring = manager.monitoring`.

**Self-disabling:** if `manager.monitoring` is unavailable, sets `self.active = False`, `self._inactive_reason = "SonarrMonitoringManager unavailable — monitoring orchestration disabled."`, and returns. Otherwise `self.active = True`.

Public methods (each a one-line delegation to a `monitoring.<submanager>`):
- `run_full_monitoring_audit()` — **defined twice**; the second definition wins (Python keeps the last). The effective body runs `monitoring.space_thresholds.run_threshold_audit()`, `monitoring.rules.evaluate_monitoring_rules()`, `monitoring.priority_queue.run_priority_queue_logic()`. (The shadowed first version returned `monitoring.audit.run_full_audit()` — dead code.)
- `schedule_priority_tasks()` → `monitoring.priority_queue.schedule_priority_queue()`
- `run_space_threshold_audit()` → `monitoring.space_thresholds.run_threshold_audit()`
- `run_rule_sync()` → `monitoring.rules.apply_rules()`
- `run_backfill_routine()` → `monitoring.backfill.backfill_all()`
- `update_episode_monitoring()` → `monitoring.episodes.adjust_monitoring_by_episode_views()`
- `update_series_monitoring()` → `monitoring.series.run_monitoring_data_pull(instance=None)`
- `run_monitoring_scheduler_update()` → `monitoring.scheduler.rebuild_priority_schedule()`
- `run_monitoring_episode_backfill()` → `monitoring.backfill.run_episode_backfill()`
- `run_monitoring_enforce_space_pressure()` → `monitoring.rules.apply_space_pressure_rules()`
- `run_monitoring_full_pipeline()` — runs `run_full_monitoring_audit` → scheduler update → episode backfill → enforce space pressure.
- `run_all()` — the decorated aggregate: space-threshold audit → full monitoring audit → rule sync → backfill → episode monitoring → series monitoring → priority tasks.

FETCH / CACHE / APPLY: this is the most APPLY-heavy orchestrator in the directory — rule application, monitoring toggles, and space-pressure enforcement all mutate Sonarr monitoring state via the leaf submanagers. It also reads thresholds/queues (FETCH) through them.

External API endpoints: none directly (the monitoring leaves issue Sonarr monitoring PUTs).

Config keys: none read directly.

global_cache / Parquet keys: none read/written directly.

dry_run: captured from kwargs/parent; the mutating decisions defer dry_run gating to the leaf monitoring submanagers.

Concurrency: none here.

## How it functions

Lifecycle: `__init__` resolves `self.monitoring` and sets the soft-disable `active` flag. Two aggregate entry points exist: `run_all()` (decorated, the primary sweep) and `run_monitoring_full_pipeline()` (an alternate ordering that includes scheduler/backfill/space-pressure enforcement). Individual verbs can also be called à la carte.

Note the duplicate `run_full_monitoring_audit`: callers get the second (space_thresholds + rules.evaluate + priority_queue) implementation, which is what both `run_all` and `run_monitoring_full_pipeline` invoke. The `monitoring.audit.run_full_audit()` path is unreachable as written — worth flagging but not changed (docs only).

Brain delegation: none directly. Monitoring *rules* and space-pressure thresholds are evaluated in the `rules`/`space_thresholds` leaves; any value-judgement they delegate goes to `machine_learning/` (not documented here).

## Criteria & examples

No literal thresholds in this file; the space/threshold numbers live in the leaf submanagers. Worked example of the pipeline effect: `run_all()` first asks `space_thresholds` whether disk is over its limit, then `rules.apply_rules()` may unmonitor low-value content, `backfill.backfill_all()` re-grabs recent missing episodes, and `episodes.adjust_monitoring_by_episode_views()` turns monitoring on/off based on how much of a series you actually watch — e.g. a show you stopped after episode 2 could have its remaining episodes unmonitored.

## In plain English

This is the "what should I keep an eye out for" department for your TV shows. On a full run it: checks whether your disk is filling up, applies your keep/skip rules, re-downloads recently-missing episodes, and adjusts which shows it keeps hunting new episodes for based on what you're actually watching. If space gets tight it tightens the rules further. Think of it as a librarian who keeps ordering new issues of the magazines you read, stops ordering the ones you've abandoned, and clears shelf space when the room gets crowded — all automatically.

## Interactions

- **Parent manager:** `SonarrManager` (resolved as `manager`); constructed by `SonarrOrchestrationManager` as its `monitoring` child (honours the `active` flag).
- **Leaf submanagers driven:** `monitoring.space_thresholds`, `monitoring.rules`, `monitoring.priority_queue`, `monitoring.backfill`, `monitoring.episodes`, `monitoring.series`, `monitoring.scheduler` (and the shadowed `monitoring.audit`).
- **Brain modules:** none directly (rule/threshold judgements delegate downstream).
