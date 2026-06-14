# SonarrSeriesSyncTautulliManager

- **File** — `scripts/managers/services/sonarr/series/sync/tautulli.py`
- **One-liner** — Pulls recent episode-watch history from Tautulli and buckets each watched title against the Sonarr library into viewed / missing / rewatch caches.

## What it does (for a senior Python engineer)

`SonarrSeriesSyncTautulliManager(BaseManager, ComponentManagerMixin)` is a submanager under `SonarrSeries`, loaded as `tautulli`. It is the Tautulli-driven counterpart to the history submanager: where `history` selects recent series from Sonarr's own event log, this class reads what was actually *watched* in Plex (via Tautulli) and cross-references it with the Sonarr library.

**Init / deps.** `parent_name = "SonarrSeries"`. After `super().__init__` + `register()`, it resolves the parent and pulls `sonarr_api`, `instance_manager`, and a **triple cache**: `global_cache`, `sonarr_cache` (from `cache_manager`/parent), and `tautulli_cache` (from `tautulli_cache` kwarg/parent). The init is decorated `@log_function_entry` and `@timeit("__init__")`.

**Public method.**
- `update_sonarr_sync_caches_from_tautulli(sonarr_instance_name: str) -> dict` (decorated `@timeit("update_sync_caches")`). FETCH+CACHE: fetches recent Tautulli titles, classifies each against the Sonarr title set, persists three caches, and returns counts `{"viewed", "missing", "rewatch"}`.

**Internal helpers.**
- `_resolve_tautulli_manager()` — returns the registry's `TautulliManager`, or lazily constructs one (with the shared logger/config/global_cache/registry), runs `check_instances()` + `process_all_data()`, registers it, and returns it.
- `_fetch_recent_titles_from_tautulli(max_entries=1000)` (decorated `@timeit("fetch_recent_titles")`) — for each Tautulli instance, calls `api.get_history(length=max_entries)`, keeps only `media_type == "episode"` rows, and aggregates per `grandparent_title` into `{count, last_played (max date), libraries (set→list)}`.

> Note: `SonarrSeriesSyncManager.composite_sync_workflow` calls `self.tautulli.get_recent_tautulli_series()` when `use_tautulli=True`. **That method is not defined in this file** — it is not present in `tautulli.py`, so either it is inherited/monkey-patched elsewhere or that path is currently unimplemented here. The public method actually defined here is `update_sonarr_sync_caches_from_tautulli`.

**FETCH endpoint.** Tautulli `get_history(length=...)` (the Tautulli `get_history` API call, via the Tautulli instance api object) — not a Sonarr endpoint.

**CACHE writes** (to `sonarr_cache`, keyed per Sonarr instance):
- `sonarr/<instance>/sync/tautulli_viewed` — titles present in both Tautulli and Sonarr, with their watch meta.
- `sonarr/<instance>/sync/tautulli_missing` — watched titles NOT found in the Sonarr library (list).
- `sonarr/<instance>/sync/tautulli_rewatches` — viewed titles with `count >= 3`.

It also reads the Sonarr title set via `self.sonarr_cache.series.get_all_titles(sonarr_instance_name)`.

**Config keys.** None read directly here.

**dry_run.** Not referenced — this class is read + cache-write only, never applies anything to Sonarr.

## How it functions

Lifecycle: construct → `register()` → inherit deps from `SonarrSeries`. When `update_sonarr_sync_caches_from_tautulli` runs: resolve/boot Tautulli → aggregate recent episode views per show title → load the Sonarr library's title set → split titles into `viewed` (in library), `missing` (not in library), and `rewatch` (viewed with `count >= 3`) → persist the three caches → log and return counts. No machine_learning delegation in this file; the rewatch threshold is a literal in the code, not a brain decision.

## Criteria & examples

- **Episode-only filter:** `_fetch_recent_titles_from_tautulli` ignores any Tautulli row whose `media_type` is not `"episode"` (movies, tracks, etc. are dropped).
- **Title classification:** a watched title in the Sonarr title set → `viewed`; otherwise → `missing`.
- **Rewatch threshold (`count >= 3`):** a show watched 4 separate times → also placed in `rewatch`. Example: "Bluey" appears in Tautulli history with `count = 5` and is in the Sonarr library → it lands in both `viewed` and `tautulli_rewatches`. A show watched twice (`count = 2`) is `viewed` but **not** a rewatch.
- **last_played:** kept as the max ISO date string across all episode views of that title.

## In plain English

This is the "what did people actually watch on Plex?" reader. It tallies every show whose episodes were played recently and how many times, then compares that list to what's in your Sonarr library. It sorts each show into three buckets: shows you have and watched, shows you watched but don't have in the library, and shows you watched three-plus times (the comfort-rewatches, like a kid looping Bluey). Those buckets get saved so other parts of the app know what's loved, what's missing, and what's a favorite — it never changes anything in Sonarr itself.

## Interactions

- **Parent manager:** `SonarrSeries`.
- **Sibling submanagers:** an alternative recent-series source to `history`; the buckets it writes can inform downstream sync/curation.
- **Services:** `TautulliManager` (resolved or lazily booted from the registry) and its instance api `get_history`; `sonarr_cache.series.get_all_titles`; `instance_manager`.
- **Brain modules:** none.
