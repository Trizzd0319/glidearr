# SonarrOrchestrationSeriesRetrievalManager

**File** — `scripts/managers/services/sonarr/orchestration/series_retrieval.py`
**One-liner** — Sequences the Sonarr series *retrieval* sub-pipeline (fetch → enrich → sync → validate), with cache-only and validation-only variants.

## What it does (for a senior Python engineer)

`SonarrOrchestrationSeriesRetrievalManager(BaseManager, ComponentManagerMixin)`. `parent_name` is derived from the class name. Unlike most siblings, its `manager` is the **`SonarrSeriesManager`** (injected by `SonarrOrchestrationManager` via the special `series_init` bundle), not the top-level `SonarrManager`.

In `__init__` it resolves the dual cache (`sonarr_cache`, `global_cache`), `sonarr_api`, `logger`, `instance_manager`, `dry_run`, and then grabs the retrieval submodules off `manager.retrieval`:
- `self.retrieval` = `manager.retrieval`
- `self.cache` = `retrieval.cache`
- `self.enrich` = `retrieval.enrich`
- `self.fetch` = `retrieval.fetch`
- `self.sync` = `retrieval.sync`
- `self.tvdb` = `retrieval.tvdb`
- `self.validate` = `retrieval.validate`

Raises `ValueError` only if it cannot resolve a logger; missing submodules are tolerated (the run methods guard each with `if self.<x>:`).

Key public methods:
- `run_full_retrieval()` — full enrichment sequence: `fetch.run()` → `enrich.run()` → `sync.run()` → then validation (`validate.validate_series_count("default")`, `validate_series_schema("default")`, `validate_series_tags("default")`).
- `run_caching_only()` — warms the series cache via `fetch.run()` and `tvdb.run()` only (no enrich/sync/validate).
- `run_validation_only()` — runs the three `validate_*("default")` checks without enrichment.

FETCH / CACHE / APPLY: drives FETCH (`fetch.run`, `tvdb.run`) and CACHE (the fetch/tvdb runs warm caches). The validation step reads, not writes. No direct APPLY.

External API endpoints: none directly (the `fetch`/`tvdb` leaves call Sonarr / TVDB).

Config keys: none read directly.

global_cache / Parquet keys: none read/written directly; the leaf `cache`/`fetch`/`tvdb` submodules own the series cache keys.

dry_run: captured from kwargs/parent; not gating anything here (no mutating APPLY).

Concurrency: none here.

## How it functions

Lifecycle: `__init__` resolves deps and submodule handles, then the three run methods each fan out to the leaf retrieval submodules in a fixed order. Each leaf call is guarded by an `if self.<submodule>:` truthiness check, so a missing submodule is silently skipped rather than crashing.

Note the hardcoded `"default"` instance passed to all validation calls in `run_full_retrieval` / `run_validation_only` — these methods do not take an `instance` parameter (contrast with `SonarrOrchestrationSeriesManager.run_series_retrieval`, which resolves a real instance and conditionally skips validation when the cache is fresh).

Brain delegation: none.

## Criteria & examples

No numeric thresholds; the logic is presence-gated. Worked example: if TVDB enrichment isn't wired (`retrieval.tvdb` is `None`), `run_caching_only()` still runs `fetch.run()` and simply skips the TVDB warm — the log shows the warmup starting and completing without a TVDB line. Conversely `run_full_retrieval()` will run all of fetch/enrich/sync but skip any of them that resolved to `None`, then always attempt the three `validate_*("default")` checks if `validate` exists.

## In plain English

This is the "go get the latest list of my shows, tidy it up, and double-check it" routine, broken into three buttons. The big button does everything: download the show list from Sonarr, fill in extra details (including from TVDB), reconcile it, then confirm the counts and tags look right. A lighter button just pre-loads the list for speed. A third button just audits what's already saved without re-downloading. It's deliberately forgiving: if one helper isn't available it just skips that step instead of failing the whole refresh.

## Interactions

- **Parent manager:** `SonarrSeriesManager` (injected as `manager`); constructed by `SonarrOrchestrationManager` as its `series_retrieval` child.
- **Leaf submodules driven:** `retrieval.fetch`, `retrieval.enrich`, `retrieval.sync`, `retrieval.tvdb`, `retrieval.validate`, `retrieval.cache`.
- **Brain modules:** none.
