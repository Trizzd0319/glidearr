# SonarrCacheMonitoringManager

- **File** — `scripts/managers/services/sonarr/cache/monitoring.py`
- **One-liner** — Manages monitored-series state: caches the monitored-series list per instance and reconciles per-episode monitoring flags against a desired state (including a "keep"-tag enforcement pass).

## What it does (for a senior Python engineer)

`SonarrCacheMonitoringManager(BaseManager, ComponentManagerMixin)` is reachable as `sonarr_cache.monitoring`.

Public methods:
- `refresh_monitored_series(instance)` — FETCH `self.sonarr_api.get_series(instance)`, filter `monitored=True`, and CACHE the result via `self.sonarr_cache.set(f"sonarr/{instance}/monitoring_series.json", monitored)`.
- `get_monitored_series(instance)` — read that cache key back (`[]` default).
- `sync_monitored_flags(series_id, desired_map)` — FETCH episodes, and for each episode whose `monitored` differs from `desired_map[ep_id]`, APPLY `self.sonarr_api.update_episode_monitoring(ep_id, desired)`.
- `detect_series_monitoring_discrepancies(series_id)` — return episodes whose `monitored` flag disagrees with `_should_be_monitored(ep)`.
- `patch_series_monitoring_state(series_id, desired_state)` — APPLY: set every episode of a series to `desired_state` (where it differs).
- `enforce_keep_tags(series_list)` — for each series carrying a `"keep"` tag, force monitoring ON for all its episodes via `patch_series_monitoring_state(series["id"], True)`.

Internal helper `_should_be_monitored(episode)` — currently a hard rule: True only for `S01E01` (the pilot). This is the baseline against which `detect_series_monitoring_discrepancies` compares.

FETCH / CACHE / APPLY: all three. FETCH (`get_series`, `get_episodes`), CACHE (`monitoring_series.json`), APPLY (`update_episode_monitoring`). External API: `self.sonarr_api.get_series`, `get_episodes`, `update_episode_monitoring`.

`dry_run`: captured in `__init__` but **not checked** in any APPLY path here — `sync_monitored_flags` / `patch_series_monitoring_state` / `enforce_keep_tags` issue live monitoring updates regardless of `dry_run`. (Noted explicitly; the dry-run-aware lifecycle logic lives in `episode_files.py`.)

Config keys: none. Cache keys: `sonarr/{instance}/monitoring_series.json` (read/write).

## How it functions

Init derives `parent_name` from the class name (`"SonarrCacheMonitoring"`), wires the dual cache + `sonarr_api`/`logger`/`manager`/`dry_run` from kwargs or the registered parent, registers, and raises without a logger. No `load_components` (no submanagers). Each method is a small FETCH-then-filter or FETCH-then-PUT loop. No decision is delegated to a `machine_learning` module — the only judgement is the inline `_should_be_monitored` pilot rule and the `"keep"` tag check.

## Criteria & examples

- `_should_be_monitored`: `S01E01` → expected monitored; everything else → expected unmonitored. So for a series where `S01E01` is currently unmonitored, `detect_series_monitoring_discrepancies` flags it as a discrepancy.
- `enforce_keep_tags`: a series with `tags=["keep", "anime"]` gets every episode's monitoring forced ON; a series tagged only `["anime"]` is left alone.
- `sync_monitored_flags(7, {201: True, 202: True})`: if episode 201 is already monitored but 202 is not, only 202 receives an `update_episode_monitoring(202, True)` call.

## In plain English

This is the clerk who keeps the "shows we are actively recording" list and makes the actual recording switches match what they should be. If you have tagged a series "keep" — say you never want to lose Bluey — this clerk makes sure every episode of it stays flagged to record. It can also spot mismatches: by its simple house rule, only the very first episode of a show is expected to be monitored, so anything else out of sync gets reported.

## Interactions

- **Parent manager:** `SonarrCacheManager` (attached as `monitoring`).
- **Services:** the `sonarr_api` gateway (`SonarrInstanceManager`) for series/episode reads and monitoring writes; `sonarr_cache` for the monitored-series JSON cache.
- **Brain modules:** none.
