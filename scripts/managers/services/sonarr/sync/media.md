# SonarrSyncMediaManager

- **File** — `scripts/managers/services/sonarr/sync/media.py`
- **One-liner** — Syncs Sonarr media-management settings across all instances, serves cached Sonarr metadata, and replicates quality profiles + custom formats from a reference instance to the rest.

## What it does (for a senior Python engineer)

`SonarrSyncMediaManager(BaseManager, ComponentManagerMixin)` handles three loosely related "make instances consistent" jobs: media-management config, metadata caching, and quality-profile/custom-format replication.

Position in the manager tree:
- **Parent** — resolved from the class name: `parent_name` becomes `"SonarrSyncMedia"` (class name minus `"Manager"`; the literal `"SonarrStorage"` default is overwritten). Falls back to the parent's `sonarr_api` / `logger` / `manager` if not injected.
- **Submanagers** — none (leaf).

FETCH / CACHE / APPLY:
- FETCH — Sonarr `config/mediamanagement` (GET), `metadata` (GET), `qualityProfile` (GET), `customFormat` (GET).
- CACHE — Sonarr metadata under `Paths.sonarr.METADATA` (`"sonarr/<instance>/metadata"`) via `get_or_generate_cache`.
- APPLY — Sonarr `config/mediamanagement` (PUT), `qualityProfile` (POST), `customFormat` (POST).

External API endpoints touched: `config/mediamanagement` (GET/PUT), `metadata` (GET), `qualityProfile` (GET/POST), `customFormat` (GET/POST).

Config keys read:
- `self.config.get("sonarr_instances", {})` (instance map, in `sync_media_management_settings`).
- `self.config.get_default_sonarr_instance_name()` (in `warm_cache`).

global_cache / Parquet keys:
- `Paths.sonarr.METADATA` (`sonarr/<instance>/metadata`) — read/generated in `get_metadata`, warmed (1-week expiry) in `warm_cache`.

dry_run behavior: `self.dry_run` is captured in `__init__`, but **none of the public methods consult it** — `sync_media_management_settings` and `sync_quality_across_instances` issue their PUT/POST calls unconditionally. (If dry-run suppression is expected here, it is not implemented in this file.)

Singleton / concurrency: BaseManager singleton. No threading.

Public methods:
- `sync_media_management_settings(settings: dict)` — for each configured instance, GETs the current `config/mediamanagement`, merges `settings` over it (`current.update(settings)`), and PUTs it back. Skips instances whose current settings aren't a dict; catches and logs per-instance errors.
- `get_metadata(instance)` → list — returns the cached/generated `metadata` list for the instance (generator returns `[]` on falsy response).
- `warm_cache(logger, cache, config)` — **staticmethod**; builds a throwaway instance and preloads the metadata cache with `expiration_time=604800` (1 week).
- `sync_quality_across_instances()` — replicates quality profiles and custom formats from a reference instance to all others (see flow).

## How it functions

`__init__` is the standard leaf pattern (BaseManager wiring, `register()`, parent/dep resolution, logger guard).

`sync_media_management_settings`:
1. Reads the `sonarr_instances` map; warns and returns if empty.
2. Per instance: GET `config/mediamanagement`; if it isn't a dict, warn + skip. Otherwise merge the caller's `settings` over the current config and PUT the merged dict. Logs ✅/⚠️ based on the truthy result; exceptions are caught and logged per instance.

`sync_quality_across_instances`:
1. Gets all instances from `self.registry.get_all("sonarr_api")`; errors out (logs) if none.
2. Picks the **first** entry as the reference instance and FETCHes its `qualityProfile` and `customFormat` lists.
3. For every other instance, POSTs each reference profile to `qualityProfile` and each reference format to `customFormat`. (Straight POST replication — no dedupe/conflict check, unlike `SonarrSyncCustomFormatsManager`.)

`warm_cache` preloads metadata; note it passes `manager.get_metadata` as the generator without an `instance` argument and constructs the manager with `cache=cache` (the keyword the throwaway instance accepts).

Brain delegation: none.

## Criteria & examples

- **Media-management merge is additive/overwriting per key.** If an instance currently has `{"renameEpisodes": false, "createEmptySeriesFolders": true}` and `settings = {"renameEpisodes": true}`, the PUT body becomes `{"renameEpisodes": true, "createEmptySeriesFolders": true}` — only the provided key is overwritten, the rest is preserved.
- **Non-dict guard.** If `config/mediamanagement` returns a non-dict (e.g. an error string), that instance logs `⚠️ Invalid media management structure ... Skipping.` and is not modified.
- **Reference instance = first.** With instances `{"sonarr-main": ..., "sonarr-4k": ...}`, `sonarr-main` (first) is the reference; its profiles/formats are POSTed to `sonarr-4k` only.
- **Metadata cache TTL.** `warm_cache` keeps metadata for `604800` seconds (1 week) before regenerating.

## In plain English

This manager keeps the "house rules" identical across all your TV servers. One job is general settings — e.g. "always rename downloaded episodes neatly" — copied to every server. A second job is remembering the server's metadata list for a week so it doesn't keep re-asking. The third job is picking one server as the gold standard and copying its quality preferences (e.g. "grab the best 1080p version of *Stranger Things*") onto the others, so no matter which server fetches a show, it makes the same quality choices.

## Interactions

- **Parent** — `SonarrSyncManager` (registered as `SonarrSyncMedia`).
- **Sibling submanagers** — `SonarrSyncCustomFormatsManager`, `SonarrSyncFoldersManager`, `SonarrSyncNamingManager`, `SonarrSyncTagsManager`.
- **Services** — Sonarr API (`config/mediamanagement`, `metadata`, `qualityProfile`, `customFormat`); `GlobalCacheManager` for the `METADATA` key; `RegistryManager` for `get_all("sonarr_api")`.
- **Brain modules** — none.
