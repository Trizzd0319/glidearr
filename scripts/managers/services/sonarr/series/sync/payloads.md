# SonarrSeriesSyncPayloadManager

- **File** — `scripts/managers/services/sonarr/series/sync/payloads.py`
- **One-liner** — A pure builder/validator that turns series metadata into a Sonarr-compatible add-series payload and checks it has the required fields.

## What it does (for a senior Python engineer)

`SonarrSeriesSyncPayloadManager(BaseManager, ComponentManagerMixin)` is a thin, stateless submanager under `SonarrSeries`, loaded as `payload`. It performs no I/O — neither FETCH, CACHE, nor APPLY — it only transforms dicts.

**Init / deps.** `parent_name = "SonarrSeries"`. Note the ordering quirk: it resolves the parent and pulls `global_cache`, `sonarr_cache`, `logger`, `sonarr_api`, `orchestration`, `dry_run` *before* calling `super().__init__(self.logger, config, self.global_cache, ...)` and `register()`. Raises `ValueError` if no logger.

**Public methods.**
- `prepare_series_payload(metadata: dict, instance: str) -> dict` (decorated `@log_function_entry`, `@timeit("prepare_series_payload")`). Resolves the instance via `self.manager.instance_manager.resolve_instance`, reads that instance's config block, and assembles a Sonarr v3 series payload. Returns the payload dict.
- `validate_series_payload(payload: dict) -> bool` (decorated `@timeit("validate_series_payload")`). Returns `True` only if all of `["tvdbId", "title", "titleSlug", "qualityProfileId", "rootFolderPath"]` are truthy in the payload; otherwise logs the missing fields and returns `False`.

**Payload shape** built by `prepare_series_payload`:
```
tvdbId, title,
titleSlug          (metadata "slug", else title.lower().replace(" ", "-"); "untitled" if no title)
qualityProfileId   (metadata "qualityProfileId", default 1)
languageProfileId  (metadata "languageProfileId", default 1)
seasonFolder       (hardcoded True)
monitored          (metadata "monitored", default False)
rootFolderPath     (instance config "rootFolderPath", default "/tv")
seriesType         (metadata "seriesType", default "standard")
tags               (metadata "tags", default [])
```

**Config keys.** Reads `config["sonarr_instances"][resolved_instance]` for `rootFolderPath`.

**global_cache / Parquet.** None read or written.

**dry_run.** Captured but irrelevant — this class never writes.

## How it functions

Lifecycle: resolve parent + deps → `super().__init__` → `register()`. Thereafter both methods are pure functions of their inputs (plus a config lookup for the root folder). `prepare_series_payload` fills defaults and derives a `titleSlug`; `validate_series_payload` is a presence check on five required fields. No machine_learning delegation.

## Criteria & examples

- **Slug derivation:** metadata `{"title": "The Last of Us"}` with no `slug` → `titleSlug = "the-last-of-us"`. With `metadata["slug"] = "the-last-of-us-2023"`, that explicit slug wins.
- **Defaults:** missing `qualityProfileId` → `1`; missing `monitored` → `False`; missing `rootFolderPath` in instance config → `"/tv"`; `seasonFolder` is always `True`.
- **Validation pass:** `{"tvdbId": 392256, "title": "The Last of Us", "titleSlug": "the-last-of-us", "qualityProfileId": 4, "rootFolderPath": "/tv"}` → returns `True`.
- **Validation fail:** the same payload with `tvdbId` absent → logs `⚠️ Payload missing required fields: ['tvdbId']` and returns `False`. Note `qualityProfileId` defaulting to `1` (truthy) means it never trips the missing check, but `0`/empty would.

## In plain English

When the app wants to add a new show to Sonarr, Sonarr expects a tidy form filled out a specific way — TVDB id, title, which quality to grab, where to store it, and so on. This little helper fills out that form from whatever show info it has, supplying sensible defaults (store under `/tv`, use a season folder, "standard" show type) for anything missing. A second helper double-checks the form has the must-have fields before it gets submitted, so a half-filled request never goes through. It doesn't talk to Sonarr itself — it just prepares and proofreads the paperwork.

## Interactions

- **Parent manager:** `SonarrSeries` (uses `self.manager.instance_manager` to resolve the instance).
- **Sibling submanagers:** produces payloads usable by the synchronize/async appliers (though `composite_sync_workflow` currently builds its own minimal `{id, tags, monitored}` jobs).
- **Services:** none beyond `instance_manager`; reads `config["sonarr_instances"]`.
- **Brain modules:** none.
