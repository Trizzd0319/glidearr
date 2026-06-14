# SonarrOrchestrationSeriesManager

**File** — `scripts/managers/services/sonarr/orchestration/series.py`
**One-liner** — High-level conductor for the Sonarr *series* lifecycle: it sequences retrieval, sync, episode-file/Parquet enrichment, scoring, active-watcher quality upgrades, and Stage-1 space-pressure downgrades.

## What it does (for a senior Python engineer)

`SonarrOrchestrationSeriesManager(BaseManager, ComponentManagerMixin)` with `parent_name = "SonarrManager"`. It is the orchestration façade in front of `SonarrSeriesManager` (`manager.series`) and that manager's `retrieval`, `sync`, `quality`, and `space_pressure` submanagers.

Resolved references in `__init__`:
- `self.manager` — the top-level `SonarrManager`.
- `self.series_manager` — `manager.series` (a `SonarrSeriesManager`); **required** (raises `ValueError` if missing).
- `self.retrieval`, `self.sync` — `series_manager.retrieval` / `series_manager.sync`; **both required** (raises `ValueError` if either is absent).

Key public methods:
- `run_series_retrieval(instance=None, full_refresh=True, validate=True)` — drives the retrieval pipeline. Calls `self.retrieval.fetch.refresh_all_series(instance=...)` which returns `(series_list, from_cache: bool)`. If the result came from a fresh disk cache (`from_cache=True`), the count/schema validation is **skipped** (the cache freshness timestamp already proves a successful sync < 24 h ago); otherwise it runs `validate.validate_series_count(instance)` and `validate.validate_series_schema(instance)`. Always finishes with `self.retrieval.series_cache.persist_letter_cache(instance)`.
- `run_series_sync(instance=None, use_tautulli=False, force_all=False)` — calls `self.sync.composite_sync_workflow(...)` to reapply tags/monitoring/etc.
- `run_episode_file_enrichment(instance=None)` — builds/maintains the episode-file Parquet used by the ML layer (see below). No-op with a warning if the `episode_files` manager is unavailable.
- `run_full_series_enrichment(instance=None)` — the canonical full pass; orchestrates retrieval → sync → episode-file enrichment → active-watcher upgrades → space-pressure downgrades.
- `run_active_watcher_upgrades(instance=None)` — delegates to `series_manager.quality.run_active_watcher_upgrades(resolved)`; returns a stats dict (`{}` on failure or when `quality` is unavailable).
- `run_space_pressure_downgrades(instance=None)` — Stage-1 TV downgrade under space pressure; delegates to `series_manager.space_pressure.run_downgrades(...)`.

FETCH / CACHE / APPLY: orchestrator-level. It triggers FETCH (refresh_all_series, episode fetches), CACHE (persist letter cache, Parquet upserts), and APPLY (quality upgrades, downgrades) — but the actual HTTP/PUT/DELETE/Parquet writes live in the leaf managers it calls.

External API endpoints: none called directly here (the leaf `fetch`/`quality`/`space_pressure` managers issue them, e.g. Sonarr `moveFiles`/quality-profile PUTs in the upgrade/downgrade paths).

Config keys read: `tv_downgrade_enabled` (default `True`) gating `run_space_pressure_downgrades`.

global_cache / Parquet keys: indirectly — it reads the series list from the shared **letter-bucketed series cache** (`manager.sonarr_cache.series.iter_all_series(resolved)`) and drives writes to the **episode_files Parquet** (`episode_files.parquet`, the per-series `watchability_score` broadcast).

dry_run: `self.dry_run` is captured from kwargs/parent. This class issues no APPLY itself; the gating happens in the leaf managers (e.g. space_pressure / quality honour dry_run).

Concurrency: none here; sequential delegation.

## How it functions

Lifecycle: `__init__` resolves and hard-validates the `series_manager`/`retrieval`/`sync` references, then the run methods sequence the work.

`run_full_series_enrichment` ordering (and *why* the order matters):
1. `run_series_retrieval(full_refresh=True, validate=True)` — refresh the catalog.
2. `run_series_sync(use_tautulli=False, force_all=False)` — reapply attributes.
3. `run_episode_file_enrichment(instance)` — this is the dense step. After resolving the instance via `self.retrieval.fetch.instance_manager.resolve_instance(instance)` and loading the series list from `sonarr_cache.series.iter_all_series(resolved)`, it calls, in order, on the `episode_files` manager (`sonarr_cache.episode_files`):
   - `run_pilot_batch(resolved, all_series)` — fetch the representative (earliest non-special) episode file for series not yet in the Parquet, up to `PILOT_BATCH_SIZE` per run (incremental fill across cycles).
   - `run_pilot_search(resolved)` — search for missing pilots, stepping the quality profile down on prior attempts.
   - `run_jit_quality_restores(resolved)` — restore JIT-upgraded episodes watched since last run.
   - `sync_from_tautulli(resolved)` — upsert watched history, compute next_episode, apply the 3-h grace period, purge Sonarr-deleted files, cleanup.
   - `run_jit_quality_upgrades(resolved)` — upgrade just the upcoming next-episode window to best quality.
   - `refresh_scores(resolved)` — compute per-series watchability scores and persist them onto the Parquet (the Sonarr twin of Radarr's `run_refresh_scores`). Wrapped in try/except so a scoring fault can't abort downstream passes.
4. `run_active_watcher_upgrades(instance)` — upgrade quality profiles for actively-watched non-kids series when free space allows.
5. `run_space_pressure_downgrades(instance)` — runs **after** `refresh_scores` so it ranks on fresh watchability scores.

Brain delegation: the value-judgements (which series are "actively watched", which low-watchability series to downgrade, the watchability score itself) are computed by the `episode_files`/`quality`/`space_pressure` leaf managers, which in turn delegate to the **`machine_learning/`** brain (the TV watchability scorer / lifecycle planners). Those modules are out of scope here and are not documented.

## Criteria & examples

- **Validation skip rule:** if `refresh_all_series` returns `from_cache=True` (disk cache < 24 h old), count/schema validation is skipped entirely. Worked example: a second enrichment run 2 hours after the first reads the fresh letter cache, `from_cache=True`, so no live re-count API call fires — the log shows `⏩ Validation skipped`.
- **Space-pressure downgrade gate:** `run_space_pressure_downgrades` first checks `config.tv_downgrade_enabled` (skips if `False`), then computes `free_gb = space_pressure.get_free_space_gb(resolved)` and the banded ceiling `(_, U) = space_pressure._space_targets(resolved)`. If `free_gb >= U` it returns `{"free_space_gb": free_gb, "action": "none"}` and does nothing. Worked example: drive total 1000 GB, `free_space_limit` floor T = 200 GB so band U = T×1.1 = 220 GB; if 250 GB free (≥ 220) it skips; if only 180 GB free (< 220) it calls `space_pressure.run_downgrades(resolved, 180)`, which demotes the lowest-watchability series toward HD-720p until free ≥ U.
- **Active-watcher upgrade guard:** if `series_manager.quality` is `None`, it logs a warning and returns `{}` rather than raising.

## In plain English

This is the producer for the "TV shows" side of your library. Each cycle it: refreshes the list of shows you have (skipping busywork if the list was just updated), re-tags and re-checks them, then fills in a detailed spreadsheet of episodes (think: every episode of The Office with its file size and whether you've watched it). It quietly fetches a sample episode for shows it hasn't catalogued yet, marks where you are in each series, and — for the show you're binge-watching right now — bumps the next few episodes up to the best available quality so they look great when you hit play. If your hard drive is getting full, it does the reverse for the shows nobody's touching: drops them to a smaller 720p copy to free room. Crucially it scores the shows *before* deciding what to shrink, so it sacrifices the show you abandoned, not the one you're three episodes into.

## Interactions

- **Parent manager:** `SonarrManager`; constructed by `SonarrOrchestrationManager` as its `series` child.
- **Sibling/leaf managers it drives:** `SonarrSeriesManager` and its `retrieval` (→ `fetch`, `validate`, `series_cache`), `sync`, `quality`, and `space_pressure` submanagers; plus `sonarr_cache.episode_files` (the Parquet manager) and `sonarr_cache.series` (the letter-bucketed series cache).
- **Brain modules:** delegates watchability scoring and lifecycle/space decisions to `machine_learning/` via those leaf managers (not documented here).
