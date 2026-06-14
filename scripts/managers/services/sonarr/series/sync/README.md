# SonarrSeriesSyncManager

- **File** — `scripts/managers/services/sonarr/series/sync/__init__.py`
- **One-liner** — The orchestrator that selects which Sonarr series are "recent" (from Sonarr history, Tautulli watch data, or the whole library), builds per-series tag/monitored payloads, and dispatches them to the async synchronizer.

## What it does (for a senior Python engineer)

`SonarrSeriesSyncManager(BaseManager, ComponentManagerMixin)` is the package entry point for the series-sync subtree. Its `parent_name` is `"SonarrSeries"`.

**Init / dependency wiring.** `__init__` calls `super().__init__(...)`, `self.register()`, then resolves its parent via `kwargs["manager"]` or `registry.get("manager", "SonarrSeries")`. It pulls down the shared deps from the parent (or kwargs): `logger`, `dry_run`, `orchestration`, `sonarr_api`, `instance_manager`. It maintains the project's **dual-cache** convention: `self.global_cache` (the cross-service `GlobalCacheManager`) and `self.sonarr_cache` (a Sonarr-scoped cache, taken from `kwargs["cache_manager"]` or the parent's `sonarr_cache`).

**Submanagers loaded** via `load_components(...)` under `registry_prefix="sonarr.series.sync"`, `api_kwarg_name="sonarr_api"`:
- `history` → `SonarrSeriesSyncHistoryManager` (recent-series selection from the Sonarr history API)
- `payload` → `SonarrSeriesSyncPayloadManager` (build/validate Sonarr add-series payloads)
- `tautulli` → `SonarrSeriesSyncTautulliManager` (recent-series selection from Tautulli watch history)
- `synchronize` → `SonarrSeriesSyncSynchronizeManager` (the APPLY worker — GET/diff/PUT each series)
- `async_` → `SonarrSeriesSyncAsyncManager` (an older whole-library async sync path)

Each is attached as an attribute (`self.history`, `self.payload`, `self.tautulli`, `self.synchronize`, `self.async_`). After loading it logs `✅ SonarrSeriesSyncManager subcomponents loaded: <names>`.

**Key public method.**
- `composite_sync_workflow(instance=None, use_tautulli=False, dry_run=None, force_all=False)` — the single orchestration entry point (decorated `@timeit("composite_sync_workflow")`). Returns `None`; its effect is dispatching sync jobs. Flow:
  1. Resolve `dry_run` (param overrides `self.dry_run`) and the instance via `instance_manager.resolve_instance`.
  2. Build `recent_series_ids`:
     - if `use_tautulli`: `self.tautulli.get_recent_tautulli_series()` returns titles, each resolved to a series id via `self.manager.retrieval.fetch.get_series_by_title`.
     - else: `self.history.get_recent_sonarr_series(resolved_instance)` returns a set of series ids directly.
  3. **First-run fallback** (only when not using Tautulli and the history path returned ∅): seed from full Tautulli watch history. It resolves `TautulliManager` from the registry and prefers `tautulli.series.get_series_completion_stats(tautulli.watch_history.get_all_history_cached())` to get watched titles; otherwise falls back to reading the `global_cache` key `tautulli/history/all` and collecting `grandparent_title`/`title` for `episode`/`show` rows. Each title is resolved to a series id.
  4. If still empty and not `force_all`, log and return.
  5. If `force_all`, replace the set with every id from `self.manager.retrieval.fetch.get_all_series(resolved_instance)`.
  6. Get the keep-tag monitor via `self.get_tag_monitor()` (tolerates `None`).
  7. For each id, FETCH the series (`self.manager.retrieval.fetch.get_series_by_id`), add `"keep"` to its tag set when `tag_monitor.is_series_tagged_keep(sid)`, and build a job `{"instance", "title", "payload": {"id", "tags", "monitored"}}`.
  8. If any jobs, run `asyncio.run(self.synchronize.run_sync_jobs(sync_jobs=..., dry_run=dry_run))`.

**FETCH / CACHE / APPLY.** This class FETCHes via its retrieval sibling and the history/tautulli submanagers; the APPLY (PUT) is delegated to `synchronize.run_sync_jobs`. It does not itself persist caches (its submanagers do).

**External APIs touched directly.** None directly — all HTTP goes through `sonarr_api` (in `synchronize`/`history`) or the retrieval sibling.

**Config keys.** None read here directly (submanagers read `sonarr_instances`).

**global_cache keys.** Reads `tautulli/history/all` only in the fallback branch.

**dry_run.** Threaded into `run_sync_jobs`, which logs "would update" lines and writes nothing.

**Singleton / concurrency.** A `BaseManager` singleton. It uses `asyncio.run(...)` to drive the async PUT loop synchronously from within this synchronous orchestrator.

## How it functions

Lifecycle: construct → `register()` → resolve parent + deps → `load_components(...)` (instantiates and attaches the five submanagers, sets `sonarr.series.sync.<name>_initialized` registry flags) → ready. The real work happens when a caller invokes `composite_sync_workflow`, which is a pure selection-and-dispatch pipeline: choose the candidate series ids, decorate each with the `keep` tag where applicable, and hand a batch of jobs to the synchronizer. There is no machine_learning delegation in this file — the "decisions" here (which series count as recent, whether to add `keep`) come from the history/tautulli submanagers and the tag monitor, not the brain.

Notable helper interactions: `self.manager.retrieval.fetch.*` (a sibling subtree under `SonarrSeries`) provides `get_series_by_title`, `get_series_by_id`, and `get_all_series`. `self.get_tag_monitor()` is inherited from `BaseManager`.

## Criteria & examples

- **Recent-series source switch.** `use_tautulli=True` drives selection off Tautulli titles; otherwise off the Sonarr history API (default).
- **First-run fallback.** If `get_recent_sonarr_series` returns an empty set (no Sonarr history timestamp recorded yet) and `use_tautulli` is False, it seeds from the *entire* Tautulli watch history so first-run tagging/monitoring still applies. Example: a brand-new install with no recorded `sonarr/<instance>/history` timestamp finds 40 watched titles in Tautulli, resolves 37 of them to Sonarr series ids, and proceeds to sync those 37.
- **Force mode.** `force_all=True` ignores the recent-set entirely and syncs every series in the library. Example: a library of 312 series → `recent_series_ids` becomes all 312 ids.
- **Keep tag.** For a series where `tag_monitor.is_series_tagged_keep(sid)` is True, the job's `tags` set gains `"keep"` on top of the series' existing tags before dispatch.
- **No-op guard.** Empty candidate set and `force_all=False` → logs `📭 No series matched sync criteria` and returns without dispatching.

## In plain English

Think of your TV library as a big shelf of shows. This manager decides *which shows on the shelf were touched recently* — either "what did anyone actually watch lately" (Tautulli) or "what did the downloader recently grab" (Sonarr history) — and on a brand-new setup with no history yet, it just grabs everything you've ever watched so nothing gets missed. For each of those shows it makes a little instruction slip ("keep this one's labels, mark it watched-protected if it's a keeper") and hands the stack of slips to a helper that actually applies them. In dry-run mode it just reads the slips aloud instead of acting on them.

## Interactions

- **Parent manager:** `SonarrSeries` (`self.manager`); reaches its sibling `retrieval.fetch` for series lookups.
- **Submanagers (loaded here):** `history`, `payload`, `tautulli`, `synchronize`, `async_`.
- **Other services:** `TautulliManager` (resolved from the registry in the fallback path) and its `series` / `watch_history` submanagers; `sonarr_api`; `instance_manager`.
- **Brain modules:** none — this orchestrator does not delegate into `machine_learning/`.
