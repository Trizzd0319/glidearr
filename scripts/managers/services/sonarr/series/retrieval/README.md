# SonarrSeriesRetrievalManager

- **File** — `scripts/managers/services/sonarr/series/retrieval/__init__.py`
- **One-liner** — The orchestrator for the Sonarr "series retrieval" pipeline: it loads the six retrieval submanagers (cache, enrich, fetch, sync, tvdb, validate) and fans `prepare()` / `run()` out across them.

## What it does (for a senior Python engineer)

`SonarrSeriesRetrievalManager(BaseManager, ComponentManagerMixin)` is a mid-tier orchestrator. It owns no domain logic itself — it wires up and drives a set of submanagers that, between them, pull series data out of Sonarr, enrich it with TVDB metadata, persist it to the letter-bucketed cache, keep it in sync, and validate it.

**Position in the manager tree**
- `parent_name = "SonarrSeries"` — its parent is the `SonarrSeries` manager, resolved from the registry (`self.registry.get("manager", "SonarrSeries")`) when not injected via `kwargs["manager"]`.
- It inherits `logger`, `config`, `global_cache`, `sonarr_cache`, `sonarr_api`, `instance_manager`, `orchestration`, and `dry_run` from that parent when not explicitly passed.
- It loads six submanagers via `load_components` under `registry_prefix="sonarr.series.retrieval"` with `api_kwarg_name="sonarr_api"`:
  - `series_cache` → `SonarrSeriesRetrievalCacheManager`
  - `enrich` → `SonarrSeriesRetrievalEnrichManager`
  - `fetch` → `SonarrSeriesRetrievalFetchManager`
  - `sync` → `SonarrSeriesRetrievalSyncManager`
  - `tvdb` → `SonarrSeriesRetrievalTVDBManager`
  - `validate` → `SonarrSeriesRetrievalValidationManager`

**FETCH / CACHE / APPLY** — itself it does none directly; it delegates. The pipeline as a whole is FETCH (fetch/sync/tvdb) + CACHE (series_cache/enrich). No APPLY (no PUT/DELETE to Sonarr) happens here.

**Public methods**
- `__init__(...)` — resolves shared deps, builds the shared `init_args` kwargs dict, calls `load_components(...)`, sets the registry flag `sonarr.series.retrieval_manager_initialized = True`, and logs the loaded component set.
- `prepare()` — iterates `self.components`; for each submanager that has a `prepare` method, calls it inside a try/except, logging a debug line on success and a warning on failure (one bad component does not abort the rest).
- `run()` — same iteration pattern, but calls each submanager's `run()` (try/except, logs error on failure). Note: not all submanagers define `run`/`prepare`; only those that do are invoked.

**Config keys** — none read directly here (config is just passed through).
**global_cache / Parquet keys** — none read/written directly here.
**dry_run** — captured (`kwargs["dry_run"]` or parent's) and threaded into every submanager's `init_args`. This manager itself mutates nothing.
**Singleton / concurrency** — standard `BaseManager` singleton; no threads spun up here (the parallelism lives inside `enrich`).

## How it functions

Lifecycle: `__init__` → resolve parent + shared deps → build `init_args` → `load_components(component_map, ...)` (which instantiates each submanager, attaches it as `self.<name>`, injects shared deps, and flags the registry) → set the initialized flag → log.

Driving methods are `prepare()` then `run()`. Both are duck-typed: they `getattr(self, name)` for each component name and only call `prepare`/`run` if present, each guarded by try/except so a single component failure is logged but non-fatal.

No decision is delegated to a `machine_learning` brain module from this orchestrator.

## Criteria & examples

The only "rules" here are the guard conditions:
- `prepare()` / `run()` skip any component that is `None` or lacks the method. Example: if `validate` defines neither `run` nor `prepare`, the loop simply skips it and moves on to `tvdb`.
- A raising component is caught. Example: if `enrich.run()` throws because TVDB is unreachable, `run()` logs `❌ Failed to run 'enrich'` and continues to `sync`, `tvdb`, `validate` rather than aborting the whole retrieval pass.

## In plain English

Think of this as the shift manager of a small workshop whose job is keeping the "what TV shows do we have" records accurate. It doesn't personally do any of the work — it has six specialists (one who phones Sonarr for the list, one who looks up extra details on TVDB, one who files everything alphabetically, one who double-checks the files, etc.). The shift manager just walks down the line saying "you go", "now you go", and if one specialist trips up, the manager notes it and keeps the line moving instead of shutting the whole workshop down.

## Interactions

- **Parent manager:** `SonarrSeries`.
- **Sibling submanagers it loads:** `SonarrSeriesRetrievalCacheManager`, `SonarrSeriesRetrievalEnrichManager`, `SonarrSeriesRetrievalFetchManager`, `SonarrSeriesRetrievalSyncManager`, `SonarrSeriesRetrievalTVDBManager`, `SonarrSeriesRetrievalValidationManager`.
- **Services/brains:** none directly; it passes `sonarr_api`, `instance_manager`, `sonarr_cache`, and `global_cache` down to the submanagers, which talk to Sonarr / TVDB. No `machine_learning` module is invoked here.
