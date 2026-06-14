# SonarrEpisodesRetrievalCacheManager

**File** — `scripts/managers/services/sonarr/episodes/retrieval/cache.py`
**One-liner** — The caching front-door for Sonarr episode data: fetches episode files, quality profiles, and per-series episode lists through `global_cache.get_or_generate_cache` (24h TTL), with warm/force-refresh helpers.

## What it does (for a senior Python engineer)

The CACHE + FETCH submanager for episodes. Every read goes through `global_cache.get_or_generate_cache(key=..., generator_function=lambda: sonarr_api._make_request(...), expiration_time=86400)`, so a miss/expiry triggers a live Sonarr GET and repopulates the cache. It declares `parent_name = "SonarrEpisodes"`.

Public methods:
- `get_all_episode_data(instance, force_refresh=False) -> list` — caches Sonarr `episodefile` under `sonarr/<instance>/episodes/all` (`CacheKeyPaths.sonarr.ALL_EPISODES`).
- `get_all_episode_profiles(instance, force_refresh=False) -> list` — caches Sonarr `qualityProfile` under `sonarr/<instance>/profiles` (`EPISODE_PROFILES`).
- `get_episodes_by_series_id(series_id, instance, force_refresh=False, log_miss=True, log_expired=True) -> list` — caches Sonarr `episode?seriesId=<id>` under `sonarr/<instance>/episodes/by_series` with `<series_id>` substituted (`EPISODES_BY_SERIES`).
- `warm_cache_for_instance(instance)` — warms **only** the quality-profiles cache; episode files are intentionally fetched lazily per-series (see inline note: Sonarr's `/episodefile` requires `?seriesId=`, so bulk warming isn't supported).
- `warm_cache_for_all_instances()` — loops `config.get_sonarr_instances()`, skipping `"default_instance"` and non-dict entries, warming each.
- `warm_all_episodes_cache()` — orchestration entry that calls `warm_cache_for_all_instances()`.
- `get_last_modified_timestamp(instance)` — returns the max `dateAdded` across all cached episode files (or `None`).
- `get_episode_by_id(episode_id, instance, fallback_title=None)` — linear scan of cached episode files for `ep["id"]`; warns and returns `None` if absent (the `fallback_title` arg is accepted but unused).
- `get_episode_count_by_series(series_id, instance, log_miss=True, log_expired=True) -> int` — `len(get_episodes_by_series_id(...))`.
- `force_refresh_all_cache_for_instance(instance)` — force-refreshes both `episodefile` and `qualityProfile` caches for the instance.

External API endpoints touched (Sonarr, lazily via generator lambdas): `episodefile`, `qualityProfile`, `episode?seriesId=<id>`.

FETCH / CACHE / APPLY: FETCH + CACHE. No APPLY.

Config keys read: `config.get_sonarr_instances()` (warm-all loop).
global_cache / Parquet keys (all in `global_cache`):
- `sonarr/<instance>/episodes/all` — read/write (`ALL_EPISODES`).
- `sonarr/<instance>/profiles` — read/write (`EPISODE_PROFILES`).
- `sonarr/<instance>/episodes/by_series` (with `<series_id>` filled) — read/write (`EPISODES_BY_SERIES`).
`force_refresh=True` deletes the relevant key before regenerating.

dry_run: not referenced — all operations are reads/cache-writes, never Sonarr mutations.

Concurrency: `BaseManager` singleton; relies on `global_cache`'s own expiry/locking semantics. TTL is hard-coded to `86400` seconds (24h) for all three caches.

## How it functions

`__init__` resolves `self.manager`, both caches, `sonarr_api`, and `instance_manager`. Every getter first calls `instance_manager.resolve_instance(instance)` to canonicalise the instance name, builds the templated cache key (`CacheKeyPaths.sonarr.<KEY>.replace("<instance>", resolved)` and, for per-series, `.replace("<series_id>", str(series_id))`), optionally deletes it on `force_refresh`, then calls `get_or_generate_cache` with the matching Sonarr GET as the generator. `get_last_modified_timestamp` and `get_episode_by_id` are convenience readers over the already-cached `episodefile` list.

The `warm_*` methods are the orchestration hooks (`warm_all_episodes_cache` is the public top); they deliberately warm only profiles, leaving episode files to lazy per-series fetches.

No decision is delegated to a `machine_learning` brain module.

## Criteria & examples

- **TTL / regeneration:** all three caches expire after 86,400 s. Worked example: `get_all_episode_data("anime")` at 09:00 caches the `episodefile` response; a call at 20:00 the same day serves from cache (under 24h); a call the next day at 10:00 (>24h) re-runs the Sonarr GET and refreshes the key.
- **Force refresh:** `get_episodes_by_series_id(88, "series", force_refresh=True)` deletes `sonarr/series/episodes/by_series` (series 88 variant) and immediately regenerates from `episode?seriesId=88`.
- **Warm-all skip rule:** in `warm_cache_for_all_instances`, an entry named `"default_instance"` or any non-dict config value is skipped (so the alias pointer isn't treated as a real instance).
- **Last-modified:** if cached episode files have `dateAdded` of `2026-05-01`, `2026-06-09`, and one missing the field, the method returns `2026-06-09` (max, ignoring the null).

## In plain English

This is the app's pantry for Sonarr episode data. Instead of phoning Sonarr every single time someone asks "what episode files do we have?" or "what quality profiles exist?", it keeps the answers on a shelf for a day. If the shelf is empty or the carton's a day past its date, it restocks by calling Sonarr fresh, then puts it back for next time. There's also a "throw it all out and re-buy" button (force refresh) and a "stock the shelves ahead of time" routine (warm) — though for episode files it only pre-stocks the lightweight quality-profile info, grabbing the bulky per-show episode lists only when actually asked. It can also answer quick questions off the shelf, like "when was the most recent episode added?" Nothing here ever changes your library — it only reads and remembers.

## Interactions

- **Parent manager:** resolves against `SonarrEpisodes` (declared `parent_name`), within the `SonarrEpisodesRetrieval` subtree.
- **Siblings:** `fetch`, `enrich`, `tvdb`, `sync`, `validate`.
- **Services it talks to:** Sonarr API (`episodefile`, `qualityProfile`, `episode?seriesId=`); `global_cache` (`get_or_generate_cache`/`delete`); `instance_manager` (resolution); `CacheKeyPaths.sonarr` for key templates; `config` (instance enumeration).
- **Brain modules:** none.
