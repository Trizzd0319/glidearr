# SonarrSeriesRetrievalCacheManager

- **File** — `scripts/managers/services/sonarr/series/retrieval/cache.py`
- **One-liner** — A thin facade over the canonical `SonarrCacheSeriesManager` (`sonarr_cache.series`): it forwards the letter-bucket cache surface so there is a single implementation and a single in-memory bucket memo, defining only the few retrieval-specific methods locally.

## What it does (for a senior Python engineer)

`SonarrSeriesRetrievalCacheManager` is **not** a `BaseManager` subclass — it is a plain class that delegates. Its reason for existing (per the class docstring): this code used to duplicate all the letter-bucket logic, which drifted out of sync with the canonical manager (different delta semantics, no shared memo) and could leave the canonical memo stale on writes. Now every bucket op routes to the one canonical implementation.

**Position in the manager tree**
- Loaded by `SonarrSeriesRetrievalManager` as the `series_cache` component.
- Holds `manager` (the retrieval orchestrator), `logger`, `global_cache`, and `sonarr_cache`, all pulled off the injected `manager`.
- The canonical target is `sonarr_cache.series` (a `SonarrCacheSeriesManager`), resolved lazily and memoized in `self.__dict__["_canon_ref"]`.

**FETCH / CACHE / APPLY** — CACHE (cache bookkeeping/delegation). No HTTP, no Sonarr APPLY.

**Delegation mechanism**
- `_resolve_canon()` — returns the cached `_canon_ref`; otherwise tries `sonarr_cache.series`, and if that lacks `load_letter_cache`, falls back to resolving `SonarrCacheSeriesManager` from the registry (`registry.get("manager", "SonarrCacheSeriesManager")`). Memoizes the result.
- `__getattr__(name)` — only fires for attributes not defined on the class. It forwards **only** the names in `_DELEGATED_METHODS`; anything else raises `AttributeError`. This deliberate allow-list prevents accidentally delegating `BaseManager` lifecycle/introspection methods.

**`_DELEGATED_METHODS`** (frozenset, forwarded to the canonical manager): `_library_dir`, `_letter_file`, `get_series_bucket_letter`, `list_cached_letters`, `clear_letter_cache`, `load_letter_cache`, `save_series_to_letter_file`, `rebuild_bucketed_series_cache`, `get_all_series_ids`, `get_cached_series_by_id`, `deduplicate_series_data`, `summarize_cache_statistics`, `iter_all_series`, `get_all_series`, `get_series_count`, `get_all_titles`, `get_series_by_title`, `get_series_by_tvdb_id`, `get_title_by_series_id`, `remove_series`.

**Locally defined methods** (overrides / retrieval-specific behavior)
- `delta_rebuild_series_cache(instance, live_series) -> dict` — delegates to the canonical content-aware delta, then preserves the legacy `"checked"` key (defaulting it to `rewritten + skipped`) that this manager's historical callers expect.
- `rebuild_individual_series_caches(instance)` — loads the enriched DataFrame via `manager.load_enriched_series_dataframe(instance)` and writes each row to its letter file through the canonical `save_series_to_letter_file`.
- `persist_letter_cache(instance)` — resolves the instance via `manager.instance_manager.resolve_instance` (callers may pass `None`); if no instance resolves, warns and returns; otherwise calls the canonical `persist_letter_cache`.

**Config keys** — none read directly.
**Cache keys** — the letter-bucket files are owned/managed by the canonical `SonarrCacheSeriesManager`; this facade only forwards to them. The enriched source for `rebuild_individual_series_caches` is the parquet produced by the enrich manager (see `enrich.md`).
**dry_run** — not handled here.
**Concurrency** — single in-memory memo (`_canon_ref`); no threading of its own. The point of the facade is to avoid a *second* memo.

## How it functions

Lifecycle: constructed with `manager=...`; deps lifted off it. The first time any forwarded attribute is accessed, `_resolve_canon()` finds and memoizes the canonical manager, after which every bucket op (`load_letter_cache`, `save_series_to_letter_file`, `get_all_series_ids`, etc.) is a straight pass-through. The three local methods add small adapters on top of canonical calls (legacy key, dataframe-driven rebuild, instance resolution before persist).

No `machine_learning` brain module is involved.

## Criteria & examples

- **Allow-list guard:** calling a delegated name like `cache.load_letter_cache("default", "b")` is forwarded; calling something not in `_DELEGATED_METHODS` (e.g. `cache.run`) raises `AttributeError` so the facade behaves like a plain object for everything else.
- **Legacy `"checked"` key:** if the canonical delta returns `{"rewritten": 3, "skipped": 34, "added": 2, "removed": 0}`, this method adds `"checked": 37` (3 + 34) so older callers that read `stats["checked"]` keep working.
- **Null-instance persist guard:** `persist_letter_cache(None)` with an `instance_manager` that resolves `None` → logs `⚠️ persist_letter_cache: no resolvable instance — skipping.` and does nothing.

## In plain English

Imagine two people both kept their own copy of the library's card catalogue and they slowly disagreed about what was on the shelves. This class fixes that: it's a receptionist who no longer keeps her own catalogue at all — every question ("what's filed under B?", "save this card", "how many shows do we have?") gets passed straight to the one official catalogue keeper. She only does three little things herself, like translating an old-fashioned request into the modern keeper's format, so nobody who used to ask the old way gets a confused look.

## Interactions

- **Parent manager:** `SonarrSeriesRetrievalManager`.
- **Delegation target (the real implementation):** the canonical `SonarrCacheSeriesManager` at `sonarr_cache.series`, also reachable via the registry under `"SonarrCacheSeriesManager"`.
- **Siblings:** the `fetch` manager reads buckets through `sonarr_cache.series` (this facade mirrors that surface); the `enrich` manager produces the enriched parquet that `rebuild_individual_series_caches` consumes.
- **Brain modules:** none.
