# SonarrSeriesRetrievalFetchManager

- **File** — `scripts/managers/services/sonarr/series/retrieval/fetch.py`
- **One-liner** — The read path for Sonarr series data: it serves series lookups from the letter-bucketed disk cache when possible and falls back to live Sonarr API calls, and owns the cache-vs-live "refresh" sync entry point.

## What it does (for a senior Python engineer)

`SonarrSeriesRetrievalFetchManager(BaseManager, ComponentManagerMixin)` is the FETCH workhorse of the retrieval pipeline. It exposes a family of getters that prefer the cache and degrade to HTTP, plus a `refresh_all_series` method that decides between skipping (fresh), delta-syncing (stale + prior data), or full-rebuilding (no prior cache).

**Position in the manager tree**
- Loaded by `SonarrSeriesRetrievalManager` as the `fetch` component.
- Pulls its deps from `kwargs["manager"]` (the retrieval orchestrator): `sonarr_cache`, `global_cache`, `sonarr_api`, `instance_manager`, `dry_run`.
- Resolves `self.series_cache` from `self.sonarr_cache.series` (the canonical `SonarrCacheSeriesManager`); if that object itself has a `.cache` attribute it unwraps one more level. This is the letter-bucketed cache it reads from.

**FETCH / CACHE / APPLY** — primarily FETCH (HTTP GET via `sonarr_api._make_request`) and cache reads. `refresh_all_series` also triggers CACHE writes (delegated to the cache manager) and a freshness-timestamp write. No APPLY.

**External API endpoints** (all via `sonarr_api._make_request(resolved_instance, endpoint, ...)`):
- `series` — full library list.
- `series?page={page}&pageSize={chunk_size}` — paginated fetch.
- `series?tvdbId={tvdb_id}` — direct TVDB lookup.
- `series/{series_id}` — single series by id.
- `metadata` — metadata config.
- `history` — series history.

**Public methods**
- `get_all_series(instance)` — loads every letter bucket (`a–z`, `0–9`, `_`) from cache; if any series are found, returns them; otherwise falls back to a live `series` API call (`fallback=[]`).
- `get_all_series_chunked(instance, chunk_size=200)` — paginates the live `series` endpoint until a short page is returned; returns the accumulated list. (Always live, no cache.)
- `get_series_by_id(series_id, instance)` — linear scan of all letter buckets matching `str(s["id"])`; returns the series dict or `None`.
- `get_series_by_tvdb(tvdb_id, instance)` — tries the live `series?tvdbId=` endpoint first; if empty, falls back to `get_series_by_tvdb_id` (cache scan).
- `get_series_by_tvdb_id(tvdb_id, instance)` — cache scan matching `str(s["tvdbId"])`; returns series or `None`.
- `get_series_by_title(instance, title)` — canonical arg order is `(instance, title)`. Delegates to `series_cache.get_series_by_title(resolved_instance, title)` when available; otherwise case-insensitive scan of the buckets. The docstring explicitly calls out the standardized arg order to avoid the title/instance swap footgun.
- `get_metadata(instance)` — live `metadata` GET, returns list (or `[]`).
- `get_series_history(instance)` — live `history` GET (`fallback=[]`).
- `_fetch_series_by_id(instance, series_id)` — internal single-series live GET (`series/{id}`); used by the sync manager.
- `get_series_tags_map(instance)` — calls `get_all_series` then returns `{series_id: tags_list}`.
- `refresh_all_series(instance=None, force=False) -> (list, bool)` — the main sync entry. Returns `(series_list, from_cache)`. See below.

**Config keys** — none read directly.
**global_cache / cache keys** — reads via the letter-bucketed `series_cache.load_letter_cache(instance, letter)`; freshness via `global_cache.timestamp_handler` keyed `("sonarr", resolved, "series_library")`. Writes go through the cache manager's `delta_rebuild_series_cache` / `rebuild_bucketed_series_cache`.
**dry_run** — captured but not gated on here (this is a read/sync path, not an APPLY path).
**Concurrency** — none in this class; the chunked/scan loops are synchronous.

## How it functions

`refresh_all_series` is the interesting control flow. Constant `SERIES_CACHE_MAX_AGE = 86400` (24 h):

1. **Freshness gate** — unless `force=True`, ask `timestamp_handler.is_fresh("sonarr", resolved, "series_library", 86400)`. If fresh **and** the cache has > 0 series, log the age and return `(list(series_cache.iter_all_series(resolved)), True)` — no API call. Any exception in the freshness check is caught and the method proceeds to a live sync.
2. **Live fetch** — `sonarr_api._make_request(resolved, "series", fallback=[])`. (Sonarr v3 `/series` always returns the full library; there is no "modified since" filter, so a full fetch is unavoidable.) If empty → warn and return `(series, False)`. If no cache manager is available → warn and return `(series, False)` (fetched but not persisted).
3. **Delta vs full rebuild** — if the cache manager exposes `get_all_series_ids` and returns a non-empty set, and it has `delta_rebuild_series_cache`, run the smart per-bucket delta (only rewrite changed buckets). Otherwise call `rebuild_bucketed_series_cache` for a full rebuild.
4. **Timestamp** — on success, `timestamp_handler.update_timestamp("sonarr", resolved, "series_library")` (errors caught/warned). Returns `(series, False)`.

No `machine_learning` brain module is invoked.

## Criteria & examples

- **Freshness skip:** cache age < 86400 s and cached count > 0 → load from disk, return `from_cache=True`. Example: a library cached 6 h ago with 1,240 series → log "fresh (1240 series, age 6h 0m) … skipping live API call" and return the disk copy.
- **Delta sync:** cache ≥ 24 h old with prior ids. Example: 1,240 cached ids vs a fresh live list of 1,242 (two adds) → only the affected letter buckets are rewritten; log reports `+2 added, -0 removed` and unchanged buckets skipped.
- **Full rebuild:** no prior cache ids → every letter file written from scratch.
- **`get_all_series` fallback:** if all 37 letter buckets are empty, it issues a single live `series` GET rather than returning `[]`.
- **`get_series_by_tvdb` two-stage:** direct `series?tvdbId=550` returns a hit → return `response[0]`; empty → warn and scan buckets via `get_series_by_tvdb_id`.

## In plain English

This is the librarian who knows where every TV show's record card is filed. If you ask "do we have Breaking Bad?", the librarian first checks the alphabetized card drawers (fast, no phone call). Only if the drawers are empty does she phone the warehouse (Sonarr) to ask directly. Once a day she also does a stock-take: if the card drawers were updated recently she trusts them and doesn't bother phoning; if they're a day stale she phones for the full list and only re-files the drawers that actually changed — she doesn't rewrite the whole filing cabinet just because two new shows arrived.

## Interactions

- **Parent manager:** `SonarrSeriesRetrievalManager`.
- **Siblings:** the `sync` manager calls this manager's `_fetch_series_by_id`; the cache manager (`series_cache`) is the object it reads buckets from and delegates rebuild/delta to.
- **Services:** `sonarr_api` (HTTP), `instance_manager` (resolve instance name), `global_cache.timestamp_handler` (freshness), `sonarr_cache.series` (the canonical `SonarrCacheSeriesManager`).
- **Brain modules:** none.
