# SonarrOrchestrationSeriesSyncManager

**File** — `scripts/managers/services/sonarr/orchestration/series_sync.py`
**One-liner** — Thin orchestration wrapper that triggers the Sonarr series *sync* workflow (tags / monitoring / library profiles), optionally enriching first.

## What it does (for a senior Python engineer)

`SonarrOrchestrationSeriesSyncManager(BaseManager, ComponentManagerMixin)` with `parent_name = "SonarrSeries"`. Like `series_retrieval`, its `manager` is the **`SonarrSeriesManager`** (injected via `series_init`).

In `__init__` it resolves `manager`, `logger`, `dry_run`, then two **required** handles off the series manager:
- `self.series_sync` = `manager.sync` (raises `ValueError` if missing)
- `self.retrieval` = `manager.retrieval` (raises `ValueError` if missing)

Key public methods:
- `run_full_sync(instance=None, use_tautulli=False, dry_run=None, force_all=False)` — resolves the effective `dry_run` (explicit arg overrides `self.dry_run`) and calls `self.series_sync.composite_sync_workflow(instance=..., use_tautulli=..., dry_run=..., force_all=...)`.
- `run_full_enrichment_and_sync(instance=None, force_all=False)` — first runs `self.retrieval.enrich.run_enrichment(instance=instance)`, then `run_full_sync(instance=instance, use_tautulli=True, force_all=force_all)`.

FETCH / CACHE / APPLY: orchestrator-level. `composite_sync_workflow` is where tag/monitoring APPLY happens; the enrich step is FETCH/CACHE. Nothing is applied directly in this file.

External API endpoints: none directly (the sync/enrich leaves call Sonarr).

Config keys: none read directly.

global_cache / Parquet keys: none read/written directly.

dry_run: explicitly threaded — `run_full_sync` takes a `dry_run` override and passes it into `composite_sync_workflow`, so the leaf sync respects it. (Per the project's `dry_run` footgun note, propagation is explicit here.)

Concurrency: none here.

## How it functions

Lifecycle: `__init__` hard-validates the `sync` and `retrieval` references, then the two run methods delegate. `run_full_enrichment_and_sync` is the "enrich-then-sync" convenience path and pins `use_tautulli=True` so the sync uses Tautulli-derived watch signals; the bare `run_full_sync` defaults `use_tautulli=False`.

Brain delegation: none in this file (sync decisions live in the `sync` leaf / downstream).

## Criteria & examples

No numeric thresholds. The notable rule is the `dry_run` resolution: `dry_run = dry_run if dry_run is not None else self.dry_run`. Worked example: a caller passing `run_full_sync(instance="default", dry_run=True)` forces a dry run even if the manager was constructed live — the underlying `composite_sync_workflow` then logs "would ..." lines and mutates nothing. Calling it with no `dry_run` arg inherits whatever the orchestration tree was built with.

## In plain English

This is the "re-file my shows correctly" button. After the app knows what shows you have, this puts the right labels on them and sets which ones should keep grabbing new episodes (for instance, keep monitoring The Mandalorian for new seasons, stop chasing a finished, fully-watched series). One variant first refreshes the details using your viewing history from Tautulli, then does the filing. And there's a built-in "practice mode" (dry run) where it tells you what it would change without actually touching anything.

## Interactions

- **Parent manager:** `SonarrSeriesManager` (injected as `manager`); constructed by `SonarrOrchestrationManager` as its `series_sync` child.
- **Leaf managers driven:** `manager.sync` (`composite_sync_workflow`) and `manager.retrieval.enrich` (`run_enrichment`).
- **Other services:** Tautulli watch history is consumed when `use_tautulli=True` (via the sync leaf).
- **Brain modules:** none directly.
