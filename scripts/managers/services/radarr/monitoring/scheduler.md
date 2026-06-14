# RadarrMonitoringSchedulerManager

**File** — `scripts/managers/services/radarr/monitoring/scheduler.py`
**One-liner** — Runs periodic Radarr monitoring-status checks with retry/backoff, reports a monitored/unmonitored/anomaly summary, and writes it to a summary log file.

## What it does (for a senior Python engineer)

`RadarrMonitoringSchedulerManager(BaseManager, ComponentManagerMixin)` is a child of `RadarrMonitoringManager` (declares `parent_name = "RadarrMonitoringManager"`). It is the orchestration/reporting wrapper for monitoring checks. It loads no submanagers and performs no APPLY to Radarr.

**Shared deps:** `radarr_api`, `instance_manager`, `dry_run` from `kwargs` or parent (`kwargs["manager"]`). Instances pass through `_resolve_instance` (instance_manager → radarr_api → literal/`"default"`).

**Key public methods:**

- `schedule_monitoring_jobs()` — entry shim; logs and immediately calls `run_scheduled_checks()`. (No actual cron/timer registration in this file — it runs the check inline.)
- `run_scheduled_checks(instance="default", max_retries=3)` — retry loop. Calls `log_monitoring_status` then `log_execution_summary`; on exception, increments `attempts`, logs a warning, and sleeps `2 ** attempts` seconds (exponential backoff: 2, 4, 8 s). After `max_retries` failures it calls `send_failure_notification()`.
- `log_monitoring_status(instance) -> dict` — FETCH `GET movie`, then counts monitored vs. unmonitored (skipping movies whose tags are in `never_unmonitor_tags`), and collects "anomalies": titles that `hasFile` AND are `not monitored` AND whose cutoff is met (`not qualityCutoffNotMet`, defaulting to `True`). Returns `{monitored_count, unmonitored_count, anomalies}`. Returns `{}` if `radarr_api` is None.
- `log_execution_summary(summary)` — writes `make_json_safe(summary)` as indented JSON to the path from config `scheduler_summary_log_file` (default `"scheduler_summary.json"`); if `enable_metrics_push` is truthy, calls `push_metrics_to_monitoring_system`.
- `push_metrics_to_monitoring_system(summary)` — placeholder; only logs.
- `send_failure_notification()` — reads config `scheduler_failure_webhook`; logs the (would-be) notification target, or warns if none configured. (No HTTP call is actually made.)

**FETCH / CACHE / APPLY:** FETCH only (`GET movie`). It writes a local JSON summary file (filesystem, not `global_cache`). No Radarr APPLY.
**API endpoints:** `movie` (GET).
**Config keys:** `never_unmonitor_tags`, `scheduler_summary_log_file` (default `scheduler_summary.json`), `enable_metrics_push` (default False), `scheduler_failure_webhook` (default None).
**global_cache / Parquet keys:** none.
**dry_run:** captured but unused — the only side effect is a local report file, never a Radarr mutation.
**Singleton/concurrency:** standard `BaseManager` singleton. The retry loop uses a blocking `time.sleep(2 ** attempts)` on the calling thread (no async/threads of its own).

## How it functions

Lifecycle: `__init__` → `super().__init__` → `self.register()` → resolve deps → debug log. No `load_components`.

Control flow: `schedule_monitoring_jobs` → `run_scheduled_checks` (retry loop) → `log_monitoring_status` (fetch + tally) → `log_execution_summary` (persist JSON, optional metrics push). On repeated failure the loop exits and `send_failure_notification` fires.

The "anomaly" detection mirrors the cutoff logic in the movies/rules siblings: a downloaded-but-unmonitored movie whose quality goal is already met is flagged as an inconsistency worth surfacing (it should arguably be unmonitored, which the movies manager would do).

No decision is delegated to a `machine_learning` brain module; this manager is reporting + retry plumbing.

## Criteria & examples

- **Anomaly** — a movie with `hasFile=True, monitored=False, qualityCutoffNotMet=False` is flagged (it has the file at target quality but is unmonitored — a state worth noting). A movie `hasFile=True, monitored=True` is just counted as monitored, not an anomaly. Note `qualityCutoffNotMet` defaults to `True` in this check, so a movie missing that field is treated as "cutoff not met" and is *not* flagged.
- **Keep-tag skip** — with `never_unmonitor_tags = [7]`, a movie carrying tag id `7` is skipped entirely from the counts and anomaly scan.
- **Retry backoff** — if `log_monitoring_status` throws three times with `max_retries=3`, the manager sleeps 2 s, 4 s, then 8 s between attempts, then calls `send_failure_notification()`.
- **Summary file** — a run yielding `{monitored_count: 412, unmonitored_count: 38, anomalies: ["The Matrix"]}` is written as indented JSON to `scheduler_summary.json` (or the configured path).

## In plain English

This is the night-shift supervisor who does a regular headcount of the movie library. It tallies how many movies are being actively watched-for vs. ignored, and raises a flag if it finds something odd — like a movie that already arrived in the quality you wanted but is somehow still marked "ignore" (the equivalent of a finished item still sitting in the "to-do" pile). It writes its findings to a little report file. If the headcount itself keeps failing, it waits a bit longer each time before retrying, and if it still can't finish, it pings whoever's on call. It only observes and reports — it doesn't move any movies around itself.

## Interactions

- **Parent:** `RadarrMonitoringManager`.
- **Siblings:** `RadarrMonitoringMoviesManager` (which actually performs the monitor/unmonitor fixes the scheduler's anomalies imply), `RadarrMonitoringRulesManager`, `RadarrMonitoringHistoryManager`.
- **Services:** Radarr API (`GET movie`), instance manager (`resolve_instance`), local filesystem (summary JSON), `make_json_safe` cache helper for serialization.
- **Brain modules:** none.
