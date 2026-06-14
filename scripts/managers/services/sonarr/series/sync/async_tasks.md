# SonarrSeriesSyncAsyncManager

- **File** — `scripts/managers/services/sonarr/series/sync/async_tasks.py`
- **One-liner** — A whole-library async syncer that walks every series across every Sonarr instance, enforces the `keep` tag, and pushes updates concurrently per instance.

## What it does (for a senior Python engineer)

`SonarrSeriesSyncAsyncManager(BaseManager, ComponentManagerMixin)` is a submanager under `SonarrSeries`, loaded as `async_`. It is functionally near-identical to `SonarrSeriesSyncSynchronizeManager`'s bulk path (`async_synchronize_series_across_instances` + `_sync_instance_series`) — a parallel/legacy implementation of full-library tag enforcement and APPLY. It does **not** have the curated-job `run_sync_jobs` method that `synchronize` has; `composite_sync_workflow` dispatches to `synchronize`, not to this manager.

**Init / deps.** `parent_name = "SonarrSeries"`. After `super().__init__` + `register()`, resolves the parent and pulls `sonarr_api`, `logger`, `orchestration`, `instance_manager`, `dry_run`, the dual cache (`sonarr_cache` from `cache_manager`/parent, `global_cache`), and the keep-tag monitor (`self.tag_monitor = self.get_tag_monitor()`). Initializes `self.sync_failures = defaultdict(list)`. Raises `ValueError` if no logger.

**Public method.**
- `async_synchronize_series_across_instances(dry_run=False, rate_limit_delay=0.2)` — *async*; decorated `@log_function_entry`, `@timeit("async_synchronize_series_across_instances")`. Builds one `_sync_instance_series` task per instance from `instance_manager.get_all_sonarr_apis()`, runs them with `asyncio.gather`, then logs any accumulated `sync_failures` (or "All series synced successfully"). Returns `None`.

**Internal helper.**
- `_sync_instance_series(instance_name, api, dry_run, rate_limit_delay)` — for each series from `api.get_all_series()`: compute `updated_tags` from current tags, add `"keep"` when `tag_monitor.is_series_tagged_keep(series_id)` (logs `🔒 Enforcing 'keep' tag`), log a tag diff if changed, then either log a dry-run line or PUT via `api.update_single_series(series_id, {id, tags, monitored}, instance_name)`. Failures append the title to `sync_failures[instance_name]`. Sleeps `rate_limit_delay` between series; logs a per-instance count at the end.

**FETCH/CACHE/APPLY.** FETCH `api.get_all_series()`; APPLY `api.update_single_series(...)`. No cache writes.

**External API.** Sonarr — `get_all_series` and `update_single_series` on each instance's api object.

**Config keys / global_cache / Parquet.** None read or written directly.

**dry_run.** Honored — when set, logs `🛑 DRY-RUN: Would update '<title>' on <instance>` and issues no PUT.

**Concurrency.** Per-instance tasks fan out via `asyncio.gather`; within an instance, series are processed sequentially with a `rate_limit_delay` (default 0.2s) pause. `sync_failures` is a per-instance `defaultdict(list)`.

## How it functions

Lifecycle: construct → `register()` → inherit deps + keep-tag monitor. On invocation, it gathers all Sonarr instance apis and launches a coroutine per instance; each coroutine walks the full series list, enforces the keep tag, and writes back any changed series (unless dry-run). At the end it summarizes failures across instances. No machine_learning delegation — the only behavioral rule is keep-tag enforcement from `tag_monitor`.

Note the slight behavioral difference from `synchronize._sync_instance_series`: this version always builds a payload and (when not dry-run) PUTs it for every series, rather than gating the write on whether tags actually changed — i.e. it has no equality-based no-op skip.

## Criteria & examples

- **Keep-tag enforcement:** a series where `tag_monitor.is_series_tagged_keep(series_id)` is True gains `"keep"`; e.g. a series with `tags={}` becomes `tags={"keep"}` and the diff is logged before the PUT.
- **Monitored preserved:** the payload sets `monitored` to the series' existing value (`series.get("monitored", False)`) — this path does not change monitoring, only (potentially) tags.
- **Dry-run:** an instance with 150 series in dry-run logs 150 "Would update" lines and writes nothing, finishing with `📦 Finished syncing 150 series`.
- **Failure tracking:** if `update_single_series` raises for "Severance" on instance `tv-4k`, the title is recorded under `sync_failures["tv-4k"]` and surfaced in the final warning block.

## In plain English

This is a "go through the entire shelf, every show, on every server, all at once" worker. For each show it makes sure the protective "keep" sticker is on the ones that should have it, then saves the change. It works through several servers in parallel but is polite within each one, pausing briefly between shows so it doesn't overload the server. If you tell it to rehearse (dry-run), it just lists what it would change. It also keeps a tally of any shows it couldn't update. It's an older, broader sibling of the focused worker that only touches the specific shows on a hand-picked list.

## Interactions

- **Parent manager:** `SonarrSeries`.
- **Sibling submanagers:** overlaps with `synchronize` (both can do full-library keep-tag enforcement); the orchestrator's `composite_sync_workflow` routes to `synchronize`, leaving this as the bulk/standalone path.
- **Services:** `sonarr_api` / per-instance api objects (`get_all_series`, `update_single_series`); `instance_manager.get_all_sonarr_apis`; keep-tag monitor from `BaseManager.get_tag_monitor`.
- **Brain modules:** none.
