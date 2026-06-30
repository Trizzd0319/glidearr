# RadarrQualityCacheManager

- **File** — `scripts/managers/services/radarr/cache/quality.py`
- **One-liner** — Caches a Radarr instance's quality profiles, custom formats, and quality definitions, with simple read accessors and a summary log.

## What it does (for a senior Python engineer)

`RadarrQualityCacheManager(BaseManager, ComponentManagerMixin)` is a thin quality-metadata adapter. It performs FETCH (GET `qualityprofile`, `customformat`, `qualitydefinition`) and CACHE (writes the three caches). It performs no APPLY.

Where it sits in the tree:
- **Parent**: `RadarrCacheManager` (`parent_name = "RadarrCacheManager"`).
- **Submanagers**: none.

Public methods:
- `refresh_quality_profiles(instance)` — FETCH `GET qualityprofile`; CACHE under `radarr/quality_profiles/<instance>`.
- `refresh_custom_formats(instance)` — FETCH `GET customformat`; CACHE under `radarr/custom_formats/<instance>` (shared with the live custom-format managers).
- `refresh_quality_definitions(instance)` — FETCH `GET qualitydefinition`; CACHE under `radarr.<instance>.quality.definitions` (try/except guarded).
- `get_quality_profiles(instance)` — reads `radarr/quality_profiles/<instance>` (default `[]`).
- `get_custom_formats(instance)` — reads `radarr/custom_formats/<instance>` (default `[]`).
- `get_quality_definitions(instance)` — reads `radarr.<instance>.quality.definitions` (default `[]`).
- `log_quality_summary(instance)` — logs counts of the three lists.

Reader/writer keys now agree: the profile/format writers and getters share one slash-delimited, per-instance key each, so a `get_*` after a `refresh_*` returns the cached data. (Previously they used different keys — `refresh_*` wrote `radarr.quality_profiles.<instance>` / `radarr.custom_formats.<instance>` while the getters read `radarr.<instance>.quality.*` — so a read never saw a refresh. `compressed=True` was also dropped: `GlobalCacheManager.set` doesn't accept it, so the write raised `TypeError` and never landed.)

External API endpoints: `GET qualityprofile`, `GET customformat`, `GET qualitydefinition`.
Config keys read: none.
Global_cache keys written: `radarr/quality_profiles/<instance>`, `radarr/custom_formats/<instance>`, `radarr.<instance>.quality.definitions`. Read: the same three keys.

`dry_run`: captured but irrelevant — this manager only does GET + cache writes (no destructive APPLY).

Singleton/concurrency: standard `BaseManager` singleton; no threading.

## How it functions

`__init__` does BaseManager wiring, `self.register()`, then resolves `radarr_api`, `instance_manager`, `manager`, and `dry_run` from kwargs-or-parent. No `run()` and no `load_components`; callers invoke refresh/get/log helpers directly. No machine_learning delegation.

## Criteria & examples

- Empty-result guard: each refresh logs a warning and skips the cache write when the API returns an empty list. Example: `refresh_quality_definitions("default")` returning `[]` logs `⚠️ No quality definitions retrieved for default` and writes nothing.
- Reader/writer now agree: after `refresh_quality_profiles("default")` populates `radarr/quality_profiles/default`, a later `get_quality_profiles("default")` returns that same cached list. Per-instance keys stay isolated (`standard`/`ultra`/`test` each get their own file).

## In plain English

Radarr has settings that describe what "good enough quality" means — e.g. "I want at least 1080p" or "I prefer this release group." This manager keeps a local copy of those rulebooks (the quality profiles, the special-format rules, and the size-per-quality definitions) so the system can check them quickly, and it can print a quick tally like "3 profiles, 12 custom formats, 20 definitions."

## Interactions

- **Parent**: `RadarrCacheManager`.
- **Siblings**: the quality definitions/profiles it caches feed quality-decision logic elsewhere in the Radarr tree (e.g. the `quality/` sub-tree's space-pressure / universe downgrade-upgrade logic).
- **Services**: `radarr_api`.
- **Brain modules**: none.
