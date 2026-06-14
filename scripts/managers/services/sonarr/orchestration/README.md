# SonarrOrchestrationManager

**File** — `scripts/managers/services/sonarr/orchestration/__init__.py`
**One-liner** — The top-of-tree orchestrator for the Sonarr "orchestration" layer: it constructs all twelve Sonarr sub-orchestrators and runs the end-to-end Sonarr enrichment pipeline (series → episodes).

## What it does (for a senior Python engineer)

`SonarrOrchestrationManager(BaseManager, ComponentManagerMixin)` is the entry point for the orchestration sub-tree under Sonarr. Its `parent_name = "SonarrManager"`, so BaseManager auto-links it to the live `SonarrManager` singleton, inheriting that tree's logger/config/cache/validator.

Responsibilities:
- Resolve the shared dependency bundle: `global_cache` (parent-linked unless explicitly passed), `sonarr_cache` (from `cache_manager` kwarg, else `manager.sonarr_cache`), `sonarr_api`, the parent `SonarrManager` (`kwargs["manager"]` else `registry.get("manager", "SonarrManager")`), `dry_run`, and `key_builder`.
- Build twelve sub-orchestrator instances from `orchestrator_map` and attach each as an attribute on `self` (e.g. `self.series`, `self.episodes`, `self.quality`).
- Track a `self.load_summary` dict recording per-component status (`✅ Loaded`, `⏭️ Inactive: <reason>`, or `⚠️ Skipped: <error>`).

Note: this manager does NOT use `ComponentManagerMixin.load_components`; it inlines its own construction loop because two of the children (`series_retrieval`, `series_sync`) must be injected with a *different* parent — `SonarrSeriesManager` (`manager.series`) — instead of the top-level `SonarrManager` that the other ten receive.

Key public methods:
- `run_full_enrichment()` — runs the full Sonarr enrichment pipeline: `self.series.run_full_series_enrichment()` then `self.episodes.run_full_episode_retrieval()`, each guarded by try/except that downgrades a failure to a warning so one stage cannot abort the other.
- `run()` — thin alias that calls `run_full_enrichment()`.

The twelve children (attribute name → class):
`cache` → SonarrOrchestrationCacheManager; `episodes` → SonarrOrchestrationEpisodesManager; `episodes_retrieval` → SonarrOrchestrationEpisodeRetrievalManager; `instance` → SonarrOrchestrationInstanceManager; `monitoring` → SonarrOrchestrationMonitoringManager; `quality` → SonarrOrchestrationQualityManager; `repair` → SonarrOrchestrationRepairManager; `series` → SonarrOrchestrationSeriesManager; `series_retrieval` → SonarrOrchestrationSeriesRetrievalManager; `series_sync` → SonarrOrchestrationSeriesSyncManager; `storage` → SonarrOrchestrationStorageManager; `validator` → SonarrOrchestrationValidatorManager.

FETCH / CACHE / APPLY: this class itself does none directly — it is pure wiring + sequencing. The work happens in the children and the leaf Sonarr managers they wrap.

External API endpoints: none directly.

Config keys: none read directly here (children read their own).

global_cache / Parquet keys: none read/written directly.

dry_run: `self.dry_run = kwargs.get("dry_run", False)` and forwarded into every child's `base_init`. This class itself has no APPLY step to gate.

Singleton / concurrency: BaseManager singleton cached by `(class, singleton_key)`. Construction is sequential; no threading here (the cache child handles parallelism).

## How it functions

Lifecycle: `__init__` → `super().__init__` (BaseManager dependency injection + parent-link) → `self.register()` → resolve shared deps → build `base_init` (the common kwargs) → derive `sonarr_init` (`base_init` + `manager=SonarrManager`) and `series_init` (`base_init` + `manager=manager.series` when present, else falls back to `sonarr_init`) → iterate `orchestrator_map`, instantiating each class.

Per-child construction is defensive: each is wrapped in try/except. After instantiation it inspects `instance.active` — if a child soft-disabled itself (set `active=False` with an `_inactive_reason`), the slot is set to `None` and recorded as inactive rather than treated as an error. Genuine exceptions become `⚠️ Skipped`. It then logs one summary line: `🧩 SonarrOrchestrationManager: N/12 sub-orchestrators loaded.`

At runtime, `run()` → `run_full_enrichment()` drives `series` then `episodes`. Note `run_full_series_enrichment` (in `series.py`) is where the heavy lifting and the ML delegation happen (scoring, downgrades), not here.

Brain delegation: none directly in this file; the value-judgement work is delegated downstream by `SonarrOrchestrationSeriesManager` (per its own doc) into the episode-file/scoring path.

## Criteria & examples

The only branching logic is the active/skip gate. Example: if `pyarrow` is missing the episode-files chain may render `episodes_retrieval` inactive — that child sets `self.active = False` and `_inactive_reason`, so `self.episodes_retrieval` becomes `None`, `load_summary["episodes_retrieval"]` = `⏭️ Inactive: ...`, and `run_full_enrichment` simply skips it (no crash). A child that throws during `__init__` (e.g. `storage` raising `ValueError` because `SonarrStorageManager` is missing) is caught and recorded as `⚠️ Skipped`, and the count line shows e.g. `11/12`.

## In plain English

Think of this as the shift manager for the whole "TV department" of the app. When it clocks in, it tries to staff twelve specialist stations (the people who fetch your show list, the people who right-size video quality, the people who clean up junk files, and so on). If one specialist can't show up today, the shift manager just notes "station closed" and keeps the rest running rather than shutting the whole department. Then, on command, it runs the day's routine in order: first refresh the catalog of your shows (say, all your Star Trek series), then go fill in episode details. The point: your TV library stays current and tidy without you babysitting it, and a single broken tool won't take everything offline.

## Interactions

- **Parent manager:** `SonarrManager` (via `parent_name`/registry).
- **Children it builds:** the twelve sub-orchestrators listed above. Ten receive `SonarrManager` as their `manager`; `series_retrieval` and `series_sync` receive `SonarrManager.series` (the `SonarrSeriesManager`).
- **Brain modules / other services:** none directly; downstream children delegate scoring/downgrade decisions into `machine_learning/` (not documented here).
