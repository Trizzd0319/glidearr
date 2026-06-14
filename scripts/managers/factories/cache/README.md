# GlobalCacheManager

- **File** — `scripts/managers/factories/cache/__init__.py`
- **One-liner** — The single process-wide cache facade that every manager receives as `global_cache`; it routes JSON, Parquet, timestamp, in-memory, diff, audit and compression work to a set of dedicated subcomponent handlers.

## What it does (for a senior Python engineer)

`GlobalCacheManager(BaseManager)` is the concrete `global_cache` object injected into every other manager in the tree (see the shared architecture note: `BaseManager.__init__` takes a `global_cache` kwarg and auto-links children to the parent's cache). It is itself constructed directly in `main.py` near the top of process startup, alongside `ConfigManager`, `RegistryManager`, and `MetricsLogger`.

Note one important deviation from the generic pattern: this class does **not** use `ComponentManagerMixin.load_components`. It instantiates its subcomponents by hand in `__init__` as plain attributes (they are *not* `BaseManager` subclasses, so they are not singletons and do not self-register in the registry). The subcomponents it builds:

| Attribute | Class | File | Role |
|---|---|---|---|
| `key_builder` | `CacheKeyBuilder` | `key_builder.py` (no `*Manager`, skipped) | Sanitizes key parts and resolves on-disk paths under the cache root |
| `json_handler` | `CacheJsonManager` | `json_handler.py` | Read/write/delete JSON cache files |
| `parquet_handler` | `CacheParquetManager` | `parquet_handler.py` | Save/load DataFrames (Parquet with CSV fallback) |
| `timestamp_handler` | `CacheTimestampManager` | `timestamp_handler.py` | Read/write `.last_updated` marker files |
| `memory` | `MemoryManager` | `memory.py` | In-process TTL cache |
| `differ` | `CacheDiffer` | `differ.py` (no `*Manager`, skipped) | DataFrame delta helper |
| `audit` | `CacheAuditManager` | `audit.py` | Enumerate / wipe cache files on disk |
| `compressor` | `CacheCompressor` | `compressor.py` (no `*Manager`, skipped) | (de)compression helper |

`self.cache_root` is taken from `key_builder.base_dir`, which resolves to `<repo>/scripts/support/cache` by default.

### Key public methods

JSON cache (the dominant API used across the codebase):
- `get(key, default=None)` — load JSON for a slash-delimited key; returns `default` if the loaded value is `None`/`{}` and a default was supplied. (Defined twice in the file — the second, default-aware definition wins.)
- `get_json(key)` — same load but goes straight through `key_builder` + `json_handler.load_json`.
- `set_json(key, data, compressed=False, pretty=True)` — JSON-safe the data (`make_json_safe`) then write; `indent=2` when `pretty`.
- `set(key, data, pretty=True)` / `set_with_pretty_output(key, data, compressed=False)` — compat write shims.
- `json_exists(key)` / `exists(key)` — existence check (the latter is a compat alias).
- `delete(key)` / `invalidate_cache_key(key)` — remove the JSON file (alias pair).
- `get_or_generate_cache(key, generator_function, ..., expiration_time=None, log_miss=True, log_expired=True, regenerate_on_expiry=False)` — the cache-or-compute workhorse (see "How it functions").

Timestamps:
- `update_timestamp(service, instance, category)` — write a fresh UTC marker; builds the key via `CacheKeyTemplate.TIMESTAMP` and delegates to `timestamp_handler.update_timestamp(path)`.
- `read_timestamp(service, instance, category)` — read it back as a `datetime`.

Parquet (enriched library DataFrames):
- `save_enriched_dataframe(df, service, instance, content_type="series")` — write a DataFrame to `{service}/{instance}/library<suffix>.parquet` where the suffix is chosen by `SUFFIX_MAP`/`EnrichedSuffix` (`series`→`_series_enriched`, `episodes`→`_episodes_enriched`, `movies`→`_movies_enriched`, `people`→`_people_enriched`).
- `load_enriched_dataframe(service, instance, content_type="series")` — the matching read.

Delta + helpers:
- `get_delta_diff(new_df, service, instance, content_type="series", primary_key="series_id", comparison_fields=None)` — compute `{"added","removed","changed"}` DataFrames against the cached enriched copy.
- `format_cache_key(service, instance, resource="")`, `build_cache_path(*parts, suffix=".json")` — thin pass-throughs to `key_builder`.
- `deduplicate_entries(existing, new_items, id_field="id", instance=None)` — merge two lists of dicts by `id_field`, newest-wins, returning `(merged_list, stats)` where stats counts `total/new/updated/skipped`.

### FETCH / CACHE / APPLY

Pure **CACHE** infrastructure. It performs no HTTP and touches no external API; it is the persistence layer that service managers call during their CACHE step (and re-read during APPLY/decision steps). The one mild exception is `get_or_generate_cache`, which *invokes a caller-supplied `generator_function`* — that callback may itself FETCH, but the cache manager neither knows nor cares.

### Config keys, cache keys, dry_run, concurrency

- **Config keys read:** none directly. It is a passive store.
- **global_cache / Parquet keys:** it does not consume keys, it *defines the key→path convention*. JSON keys are slash-delimited (`"radarr/main/library"` → `support/cache/radarr/main/library.json`). Parquet enriched keys follow `CacheKeyTemplate.SERIES_LIBRARY` + an `EnrichedSuffix`. Timestamp keys follow `CacheKeyTemplate.TIMESTAMP`.
- **dry_run:** not applicable — there are no APPLY/PUT/DELETE-to-service operations here. Writes to the local cache always happen (dry_run is about mutating external services, not the local cache).
- **Singleton/threading:** as a `BaseManager`, it is a process-wide singleton keyed by `(GlobalCacheManager, singleton_key)`, so all managers genuinely share one cache object. The subcomponents are plain (non-singleton) helpers held only by this instance. There is no internal locking; `MemoryManager`'s dict-based store is not thread-safe, so concurrent writers would need external coordination.

## How it functions

Lifecycle: `__init__` calls `super().__init__(..., global_cache=None, ...)` (it cannot inject *itself* as its own cache), then constructs `key_builder` first and threads its `base_dir` into the JSON, parquet, and timestamp handlers so they all agree on the cache root. A debug line confirms initialization. There is no `run()` entry point — it is called on demand for the life of the process.

The non-trivial control flow lives in **`get_or_generate_cache`**:
1. If `expiration_time` is set and the file exists, compute `file_age`. If within TTL, return the cached copy immediately.
2. If expired, set `expired=True` and log (info if `regenerate_on_expiry`, else debug "serving stale").
3. **Short-circuit:** if the JSON exists and it is *not* (`expired and regenerate_on_expiry`), return the on-disk copy. This is the deliberate legacy "serve-stale-forever" path that most callers depend on — without opting into `regenerate_on_expiry`, an expired key is still served from disk and the generator never runs.
4. Otherwise log a miss (if `log_miss`) and call `generator_function()`.
5. **Generator returned `None`:** treated as failure (e.g. a rate-limited Trakt fetch), *not* an empty result. If a prior file exists, the last-good copy is served and the cache is **not** overwritten. If no prior copy exists, `None` is returned as the legitimate cache-miss signal (logged at debug so callers know to handle `None`).
6. **Generator returned data (including `[]`/`{}`):** JSON-safe it and write (pretty or compact). Empty collections are cached intentionally so a valid empty API response does not become a permanent miss on every bulk run.

`get_delta_diff` sets both DataFrames' index to `primary_key`, uses index set-difference for added/removed, and (when `comparison_fields` is given) an element-wise inequality reduced with `.any(axis=1)` for the changed set. Missing prior cache or a missing primary key both degrade gracefully to "everything is new."

This class delegates **no** decision to a `machine_learning` brain module — it is infrastructure that brain-driven managers read from and write to.

## Criteria & examples

- **TTL / serve-stale rule.** With `expiration_time=86400` (1 day) and a file last modified 90,000 s ago (~25 h): the file is expired. If the caller did *not* pass `regenerate_on_expiry=True`, step 3 still returns the 25-hour-old copy and the generator never runs. If the caller *did* pass `regenerate_on_expiry=True` (e.g. the Trakt watched-set), the generator runs; if it yields fresh data the file is overwritten, but if it yields `None` (rate-limited) the 25-hour-old copy is served unchanged.
- **Empty-vs-None rule.** A generator returning `[]` for `"trakt/main/recommended"` *writes* `[]` to disk — future runs hit the cache. A generator returning `None` writes nothing and serves the prior copy if one exists.
- **Deduplicate-by-id.** `deduplicate_entries([{"id":1,"t":"a"}], [{"id":1,"t":"b"},{"id":2,"t":"c"},{"t":"x"}])` returns a merged list `[{"id":1,"t":"b"},{"id":2,"t":"c"}]` with stats `{"total":2,"new":1,"updated":1,"skipped":1}` — id 1 was updated to the newer `"b"`, id 2 is new, the id-less item is skipped.

## In plain English

Think of this as the household's shared fridge with a labeling system. When any part of the app needs something — say the list of every Marvel movie it already knows about — it asks the fridge first. If the item is there and still fresh, it grabs it (fast, no shopping trip). If it is missing, the app does the work (a "shopping trip" to Trakt or Radarr), then *labels and stores the result in the fridge* so next time is instant. There is a deliberate quirk: most labels say "use even if a little past date" — better to serve last night's leftovers than send everyone hungry — and if a shopping trip comes back empty-handed (the store was closed), it keeps the old food rather than throwing it out and leaving nothing. Everyone in the house uses the exact same fridge, so nobody buys duplicates.

## Interactions

- **Parent manager:** none in the usual sense — it is constructed directly by `main.py` and then *handed down* to every other manager as their `global_cache`. It is a peer of `ConfigManager`/`RegistryManager`/`MetricsLogger` at the root.
- **Subcomponents it owns (this directory):** `CacheJsonManager`, `CacheParquetManager`, `CacheTimestampManager`, `CacheAuditManager`, `MemoryManager`, plus the non-manager helpers `CacheKeyBuilder`, `CacheDiffer`, `CacheCompressor`. See the per-file docs.
- **Consumers:** essentially every service and lifecycle manager (Sonarr, Radarr, Trakt, Tautulli, enrich daemon, the ML-driven planners) reads/writes through this object.
- **Brain modules:** none directly. The `machine_learning/` planners consume the Parquet/JSON this manager persisted, but the cache manager itself makes no value judgements.
