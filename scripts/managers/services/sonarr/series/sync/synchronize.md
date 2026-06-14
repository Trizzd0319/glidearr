# SonarrSeriesSyncSynchronizeManager

- **File** — `scripts/managers/services/sonarr/series/sync/synchronize.py`
- **One-liner** — The APPLY worker: takes pre-built sync jobs (or every series across instances), enforces the `keep` tag, and PUTs the changed series back to Sonarr — skipping no-op writes and honoring dry-run.

## What it does (for a senior Python engineer)

`SonarrSeriesSyncSynchronizeManager(BaseManager, ComponentManagerMixin)` is the submanager under `SonarrSeries` (loaded as `synchronize`) that actually mutates Sonarr. It is the APPLY half of the sync subtree (the FETCH/diff are inline within its own methods).

**Init / deps.** `parent_name = "SonarrSeries"`. After `super().__init__` + `register()`, resolves the parent and pulls `sonarr_api`, `instance_manager`, the keep-tag monitor (`self.tag_monitor = self.get_tag_monitor()`), the dual cache (`global_cache`, `sonarr_cache` from `cache_manager`/parent), and `dry_run`. Initializes `self.sync_failures = defaultdict(list)`. If `sonarr_api` or `instance_manager` is missing it logs a warning that sync operations are unavailable (does not raise).

**Public methods.**
- `run_sync_jobs(sync_jobs, dry_run=False, rate_limit_delay=0.2)` — *async*; decorated `@log_function_entry`, `@timeit("run_sync_jobs")`. The method `composite_sync_workflow` invokes (via `asyncio.run`). Applies a pre-built list of jobs `{"instance", "title", "payload": {"id", "tags", "monitored"}}`. Returns `None`; logs an applied/unchanged/failed tally.
- `async_synchronize_series_across_instances(dry_run=False, rate_limit_delay=0.2)` — *async*; decorated. A whole-library path: iterates every Sonarr api from `instance_manager.get_all_sonarr_apis()` and syncs all series on each via `_sync_instance_series`. (Mirrors the `async_tasks.py` manager; appears to be the older bulk path.)

**Internal helper.**
- `_sync_instance_series(instance_name, api, dry_run, rate_limit_delay)` — for each series from `api.get_all_series()`, computes `updated_tags` (adds `keep` when `tag_monitor.is_series_tagged_keep`), builds `{id, tags, monitored}`, logs a diff, and either logs a dry-run line or calls `api.update_single_series(series_id, payload, instance_name)`; failures append the title to `sync_failures[instance_name]`. Sleeps `rate_limit_delay` between series.

**`run_sync_jobs` per-job flow (non-dry):**
1. Skip jobs with `payload["id"] is None`.
2. GET the full series: `sonarr_api._make_request(instance_name, f"series/{sid}", fallback=None)`; raise if not a dict.
3. Compute `new_tags`/`new_mon` from the job (`want_*`), falling back to the series' current values when the job left them `None`.
4. **No-op skip:** if `set(series.tags) == set(new_tags)` and `bool(series.monitored) == new_mon`, increment `skipped` and continue (logs `↔️ already in sync`).
5. Otherwise set `series["tags"]`/`series["monitored"]` and PUT: `sonarr_api._make_request(instance_name, f"series/{sid}", method="PUT", payload=series)`. Increment `applied`.
6. Exceptions → `failed += 1` and the title is recorded in `sync_failures`.
7. `await asyncio.sleep(rate_limit_delay)` between jobs (and `sleep(0)` on the dry/skip paths to yield).

**FETCH/CACHE/APPLY.** FETCH: GET `series/{id}`; APPLY: PUT `series/{id}`. No cache writes here.

**External API endpoints.** Sonarr `GET series/{id}` and `PUT series/{id}` via `sonarr_api._make_request`; the bulk path uses `api.get_all_series()` and `api.update_single_series(...)`.

**Config keys.** None read directly.

**dry_run.** Fully honored: every write path logs a `🛑 DRY-RUN: would update ...` line and performs no GET/PUT; in `run_sync_jobs` dry-run still increments `applied` (so the tally reflects intended writes).

**Concurrency.** Async methods; `run_sync_jobs` processes jobs sequentially with a fixed inter-job delay (`rate_limit_delay`, default 0.2s) for Sonarr rate-limiting. `async_synchronize_series_across_instances` fans out per-instance tasks with `asyncio.gather`.

## How it functions

Lifecycle: construct → `register()` → inherit deps + tag monitor. The primary path used by the orchestrator is `run_sync_jobs`: for each job it re-fetches the authoritative series object, diffs the desired tags/monitored against current state, writes only when something changed, and tallies `applied / skipped / failed`. The legacy `async_synchronize_series_across_instances` + `_sync_instance_series` path walks the entire library per instance instead of acting on a curated job list. No machine_learning delegation — the only "decision" is the keep-tag enforcement (from `tag_monitor`) and the equality-based no-op skip.

## Criteria & examples

- **Keep-tag enforcement:** a series flagged keep by `tag_monitor.is_series_tagged_keep(sid)` gets `"keep"` added to its tag set before the write.
- **No-op skip (the efficiency guard):** job wants `tags={"keep"}, monitored=True`; the live series already has `tags={"keep"}, monitored=True` → counted as `skipped`, no PUT issued. This avoids needless Sonarr writes.
- **Real change:** live series `tags={}` / `monitored=False`, job wants `tags={"keep"}` / `monitored=True` → the series dict is mutated and PUT back; counted as `applied`.
- **Missing job id:** `payload["id"] is None` → job is silently skipped (no counters touched).
- **Failure handling:** a PUT raising (e.g. 5xx) records the title in `sync_failures[instance]` and increments `failed`; processing continues with the next job.
- **Dry-run tally:** 8 jobs in dry-run → logs 8 "would update" lines and reports `8 applied ... (dry-run)` while writing nothing.

## In plain English

This is the hands-on worker at the end of the line. It's handed a stack of "make this show look like X" instructions. For each one it first looks up the show's current state in Sonarr, and if the show is already exactly how it should be, it skips it (no point re-doing work). If something differs — say a beloved show needs its protective "keep" sticker, or it should be marked as actively watched — it updates just that show and moves on, pausing a fraction of a second between each so it doesn't hammer the server. In dry-run it narrates every change it *would* make without touching anything, and it keeps a list of any shows it couldn't update so problems are visible.

## Interactions

- **Parent manager:** `SonarrSeries`; primary caller `SonarrSeriesSyncManager.composite_sync_workflow` (via `asyncio.run(run_sync_jobs(...))`).
- **Sibling submanagers:** consumes the job list the orchestrator builds (with help from `history`/`tautulli`/`payload`).
- **Services:** `sonarr_api` (`GET`/`PUT series/{id}`, `get_all_series`, `update_single_series`); `instance_manager` (`get_all_sonarr_apis`); the keep-tag monitor from `BaseManager.get_tag_monitor`.
- **Brain modules:** none.
