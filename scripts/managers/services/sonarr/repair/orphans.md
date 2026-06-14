# SonarrRepairOrphansManager

**File** — `scripts/managers/services/sonarr/repair/orphans.py`
**One-liner** — Detects and purges orphaned entries in Sonarr's local cache — episodes whose series is gone, and series with no episodes — operating entirely on the cache (not the live API).

## What it does (for a senior Python engineer)

`SonarrRepairOrphansManager(BaseManager, ComponentManagerMixin)` is a leaf repair sub-manager under `SonarrRepairManager`. It works purely against `global_cache`: **FETCH** (read cache) + **CACHE** (rewrite cache). It does not call the Sonarr HTTP API.

- **Parent:** `self.parent_name = "SonarrRepair"`. Constructed by `SonarrRepairManager` (non-critical).
- **Deps:** only the standard injected ones; no API/instance manager is required.
- **Loads submanagers:** none.

Public methods:

- **`scan_orphaned_cache_entries(instance_name=None)`** — reads the library cache (`CacheKeyPaths.sonarr.LIBRARY`) and the episodes cache (`CacheKeyPaths.sonarr.EPISODES`) for the instance, derives `library_ids` (series ids) and `episode_ids` (the episode cache's keys), then flags: episodes whose key is not a known series id → `orphaned["episodes"]`; series ids with no episode whose `seriesId` matches → `orphaned["series"]`. Returns `{"series": [...], "episodes": [...]}`.
- **`purge_orphaned_cache(orphaned_data, instance_name=None, dry_run=False)`** — if `dry_run`, returns `{"dry_run": True, "purged_series": 0, "purged_episodes": 0}` without changes. Otherwise removes the flagged series from the library cache's `series` list and the flagged episode keys from the episodes cache, writes both back via `global_cache.set`, sets the registry flag `sonarr.repair.orphans.last_purge` to a timestamp, and returns counts plus the `keys_updated` list.
- **`run_full_orphan_check(instance_name=None, auto_purge=True, dry_run=False)`** — orchestrates: scan, then (if `auto_purge`) purge. Returns `{"orphaned": ..., "purged": ...}`.

- API endpoints touched: none.
- Config keys read: none.
- global_cache keys: reads/writes `sonarr/<instance>/library` (`CacheKeyPaths.sonarr.LIBRARY`) and the episodes key referenced as `CacheKeyPaths.sonarr.EPISODES`.
- FETCH / CACHE / APPLY: **FETCH + CACHE** (cache-only; no live APPLY to Sonarr).
- dry_run: this manager takes `dry_run` as a **method argument** (not from the manager chain). When true, `purge_orphaned_cache` mutates nothing.
- Singleton/threading: standard `BaseManager` singleton; no threading.

**Note (possible latent bug):** the code references `CacheKeyPaths.sonarr.EPISODES`, but the `CacheKeyPaths.sonarr` class defines `ALL_EPISODES`, `FUTURE_EPISODES`, and `EPISODES_BY_SERIES` — no attribute named `EPISODES`. Unless that attribute is added elsewhere, the episodes-key lines would raise `AttributeError` at call time.

## How it functions

Lifecycle: `__init__` sets `parent_name`, calls `super().__init__`, `self.register()`, logs an init line. The control flow is scan → (optional) purge, all in terms of the two cache blobs (`{"series": [...]}` for the library, a dict keyed by episode id for episodes). The purge is the only mutating step and is fully gated by its `dry_run` argument; it also records `sonarr.repair.orphans.last_purge` in the registry. No `machine_learning` brain module is involved.

## Criteria & examples

- **Orphaned episode:** an episode key not present among library series ids. Example: episodes cache has key `7001` but the library has series ids `{10, 11}` → `7001` is an orphaned episode.
- **Orphaned series:** a series id with no episode pointing back to it. Example: library has series id `11` but no cached episode has `seriesId == 11` → `11` is an orphaned series.
- **Dry-run guard:** calling `purge_orphaned_cache(data, dry_run=True)` returns `{"dry_run": True, "purged_series": 0, "purged_episodes": 0}` and touches no cache key.

## In plain English

Imagine a paper catalog of your shows kept separately from the actual shelf. Over time the catalog can drift: it might list episode cards for a show that's no longer in the catalog at all, or list a show with zero episode cards. This specialist combs the catalog (not the real shelf) and either reports those mismatches or tidies them out — but only if you tell it to actually purge, and never in practice mode. It also jots down the date it last cleaned up.

## Interactions

- **Parent manager:** `SonarrRepairManager`.
- **Siblings:** the other `SonarrRepair*Manager` specialists.
- **Services:** `global_cache` only (library + episodes keys); the `registry` for the last-purge timestamp flag.
- **Brain modules:** none.
