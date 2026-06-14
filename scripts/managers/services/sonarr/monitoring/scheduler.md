# SonarrMonitoringSchedulerManager

- **File** — `scripts/managers/services/sonarr/monitoring/scheduler.py`
- **One-liner** — The monitoring "health check" runner: it tallies monitored vs. unmonitored series (flagging anomalies like still-running shows that aren't being recorded), persists a JSON summary, and retries with backoff + failure notification.

## What it does (for a senior Python engineer)

`SonarrMonitoringSchedulerManager(BaseManager, ComponentManagerMixin)`, `parent_name = "SonarrMonitoring"`. Resolves parent from registry/`manager` kwarg; pulls `sonarr_api`, `manager`, `logger`, `dry_run`, `sonarr_cache`, `global_cache`, and `self.tag_monitor = self.get_tag_monitor()` (keep-tag guard). Raises `ValueError` if no logger. Loads no submanagers.

It is primarily a **FETCH** + report/persist manager — it reads series state and writes a local JSON summary file. No Sonarr APPLY, no cache writes.

Public methods:
- `schedule_monitoring_jobs()` — entry point; logs and calls `run_scheduled_checks()`. (Despite the name, it does not register a cron/timer here — it runs the check immediately.)
- `run_scheduled_checks(max_retries=3)` — retry loop with exponential backoff (`time.sleep(2 ** attempts)`): calls `log_monitoring_status()` then `log_execution_summary(...)`; on exception, increments attempts and retries; after `max_retries` failures it calls `send_failure_notification()`.
- `log_monitoring_status() -> dict` — iterates `sonarr_api.get_all_series()`, skips `keep`-tagged series (`tag_monitor.is_series_tagged_keep`), counts monitored/unmonitored, and collects an **anomaly** list of titles that are unmonitored but whose Sonarr `status == "continuing"`. Returns `{monitored_count, unmonitored_count, anomalies}`.
- `log_execution_summary(summary)` — writes `make_json_safe(summary)` to the path in `config["scheduler_summary_log_file"]` (default `scheduler_summary.json`); if `config["enable_metrics_push"]` is truthy, calls the metrics-push placeholder.
- `push_metrics_to_monitoring_system(summary)` — **placeholder** (logs only; comment notes a Prometheus push gateway could go here).
- `send_failure_notification()` — reads `config["scheduler_failure_webhook"]`; if set, logs that it would notify (a **placeholder** — comment notes `requests.post` integration); otherwise warns no webhook configured.

**API touched:** `sonarr_api.get_all_series()`.
**Config keys read:** `scheduler_summary_log_file` (default `scheduler_summary.json`), `enable_metrics_push` (default `False`), `scheduler_failure_webhook`.
**Filesystem written:** the JSON summary file at `scheduler_summary_log_file`.
**Cache keys:** none read/written.
**dry_run:** captured into `self.dry_run` but **not consulted** — the manager performs no Sonarr mutations, so dry_run is moot; it always writes the local summary file regardless.

## How it functions

Lifecycle: `__init__` (resolve deps incl. `tag_monitor`, logger guard) → `register()`. Run path: `schedule_monitoring_jobs()` → `run_scheduled_checks()` → (retry) `log_monitoring_status()` + `log_execution_summary()` → on exhaustion `send_failure_notification()`. The backoff sleeps 2, 4, 8 seconds on attempts 1, 2, 3. The anomaly detection (`continuing` show that is unmonitored) is the only "judgement," and it's a plain status comparison — no `machine_learning` brain module is consulted. The metrics-push and webhook integrations are explicitly stubbed.

## Criteria & examples

- **Keep-tag skip:** any series where `tag_monitor.is_series_tagged_keep(sid)` is true is skipped entirely (not counted, not anomaly-checked).
- **Anomaly rule:** unmonitored AND `status == "continuing"` → anomaly. Example: *Abbott Elementary* (`status="continuing"`) is unmonitored → flagged as an anomaly ("⚠️ Unmonitored continuing series: Abbott Elementary"). An unmonitored *Breaking Bad* (`status="ended"`) is **not** an anomaly (it's expected that a finished show stops recording).
- **Retry/backoff:** if `log_monitoring_status` throws twice then succeeds, the run logs two warnings, sleeps 2s then 4s, and ultimately succeeds without notifying. If all 3 attempts throw, it logs an error and calls `send_failure_notification()`.
- **Summary persistence:** a run with 42 monitored, 8 unmonitored, 1 anomaly writes `{"monitored_count":42,"unmonitored_count":8,"anomalies":["Abbott Elementary"]}` to `scheduler_summary.json`.

## In plain English

This is the daily roll-call clerk for your TV recordings. It counts how many shows are set to record vs. not, and raises a hand whenever something looks wrong — specifically, a show that's still airing new episodes but isn't being recorded (that's probably a mistake). It writes the headcount to a little report file. If the check itself crashes, it tries again a few times (waiting a bit longer each time) and, if it still can't finish, fires off a "something's broken" alert. It deliberately leaves alone any show you've marked "keep." It doesn't change any recordings itself — it's the inspector, not the technician.

## Interactions

- **Parent:** `SonarrMonitoring` (`SonarrMonitoringManager`).
- **Managers it talks to:** `SonarrSyncTagsManager` via `get_tag_monitor()` for the keep-tag skip.
- **Services:** Sonarr API (series fetch). Optional/stubbed: a metrics push gateway and a failure webhook (both placeholders).
- **Brain modules:** none.
