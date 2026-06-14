# SonarrCacheQualityManager

- **File** — `scripts/managers/services/sonarr/cache/quality.py`
- **One-liner** — Caches a Sonarr instance's quality profiles, custom formats, and quality definitions as JSON, with simple refresh/get accessors and a summary log.

## What it does (for a senior Python engineer)

`SonarrCacheQualityManager(BaseManager, ComponentManagerMixin)` is reachable as `sonarr_cache.quality`. It is a straightforward FETCH-then-CACHE wrapper over three Sonarr quality-related endpoints.

Public methods (all keyed by `instance`):
- `refresh_quality_profiles(instance)` — FETCH `self.sonarr_api.get_quality_profiles(instance)`, CACHE to `sonarr/{instance}/quality_profiles.json` (warn if empty).
- `get_quality_profiles(instance)` — read that key back (`[]` default).
- `refresh_custom_formats(instance)` — FETCH `get_custom_formats`, CACHE to `sonarr/{instance}/custom_formats.json`.
- `get_custom_formats(instance)` — read back.
- `refresh_quality_definitions(instance)` — FETCH `get_quality_definitions`, CACHE to `sonarr/{instance}/quality_definitions.json`.
- `get_quality_definitions(instance)` — read back.
- `log_quality_summary(instance)` — read all three caches and log entry counts.

FETCH / CACHE / APPLY: **FETCH + CACHE only** (no writes back to Sonarr). External API: `get_quality_profiles`, `get_custom_formats`, `get_quality_definitions` (Sonarr's quality endpoints via the gateway). Cache keys: the three `*.json` files above (`global_cache.set`/`.get`). Config keys: none.

`dry_run`: captured in `__init__`; not relevant here because nothing is applied to Sonarr (caching local metadata is non-destructive).

## How it functions

Init computes `parent_name` from the class name, wires dual cache + `sonarr_api`/`logger`/`manager`/`dry_run`, registers, raises without a logger. No `load_components` (no submanagers). Each refresh method is FETCH → if-non-empty CACHE → log; each get is a cache read. No decision is delegated to a `machine_learning` module.

Note: file-size estimation in the wider system does **not** read these quality definitions directly — per the project's "unified size model" design, size estimation funnels through `machine_learning/sizing/size_model.py`. This manager just mirrors Sonarr's quality metadata into cache.

## Criteria & examples

- `refresh_quality_profiles` only writes the cache when the API returns a non-empty list; an empty/None response logs `⚠️ No quality profiles retrieved` and leaves the existing cache intact.
- `log_quality_summary("default")` on a typical instance logs something like: Profiles: 6 entries, Custom Formats: 24 entries, Quality Definitions: 19 entries.

## In plain English

Sonarr knows what "good quality" means for you — your quality profiles (e.g. "HD 1080p", "Ultra-HD"), your custom format rules (prefer this release group, avoid that one), and the size definitions for each tier. This clerk keeps a local photocopy of all of that so the rest of the app can read your quality preferences quickly without phoning Sonarr every time, the way you might keep a printed copy of a streaming service's video-quality settings.

## Interactions

- **Parent manager:** `SonarrCacheManager` (attached as `quality`).
- **Services:** the `sonarr_api` gateway (`SonarrInstanceManager`) for the three quality endpoints; `global_cache` for the JSON caches.
- **Brain modules:** none directly.
