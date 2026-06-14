# SonarrQualityCustomFormatsManager

- **File** — `scripts/managers/services/sonarr/quality/custom_formats.py`
- **One-liner** — Reads, adds, and syncs Sonarr "Custom Formats" (the named scoring rules Sonarr uses to prefer/avoid release traits) across one or more Sonarr instances.

## What it does (for a senior Python engineer)

`SonarrQualityCustomFormatsManager(BaseManager, ComponentManagerMixin)` is a FETCH + APPLY + CACHE leaf submanager under the Sonarr quality tree. It is the adapter for the Sonarr `customformat` and `qualityProfile` endpoints.

Key public methods:
- `get_custom_formats(instance)` → list of custom formats. CACHE-backed read: `global_cache.get_or_generate_cache(key=CacheKeyPaths.sonarr.CUSTOM_FORMATS, generator_function=lambda: _make_request(resolved_instance, "customformat") or [])`.
- `add_custom_format(instance, custom_format_payload)` → APPLY: `POST customformat` with the payload.
- `get_custom_format_scores(instance)` → `{cf_name: score}` mapping from a live `customformat` fetch (empty dict if none).
- `sync_custom_formats(instance, new_custom_formats)` → APPLY loop: fetches + dedupes existing CFs, then `add_custom_format` for each new CF whose name doesn't already exist; logs an Added/Skipped/Total summary.
- `get_profile_scores_by_format(instance)` → `{format_id: summed_score}` over all quality profiles' enabled `formatItems`.
- `log_unused_custom_formats(instance)` → logs CFs whose `id` is not referenced (as a scored format) by any profile.
- `run_custom_format_data_pull(instance)` → CACHE step: for every Sonarr instance, raw-HTTP `GET {base_url}/api/v3/customformat` and persist to global_cache.
- Private helpers: `_custom_format_exists(name, formats)` (case-insensitive name match), `_deduplicate_custom_formats(formats)` (first-seen dedupe by lowercased name).

Position in the tree: child of **SonarrQualityManager** (derives `self.parent_name` by stripping `"Manager"` → `"SonarrQualityCustomFormats"`, then resolves its actual parent from the registry). Loads no submanagers.

FETCH / CACHE / APPLY:
- FETCH: `_make_request` against `customformat` and `qualityProfile`; raw `requests.get` in the data-pull.
- CACHE: `get_custom_formats` reads through `get_or_generate_cache`; `run_custom_format_data_pull` writes.
- APPLY: `add_custom_format` (`POST`) and therefore `sync_custom_formats`.

External API endpoints touched:
- Sonarr REST via `_make_request`: `customformat` (GET and `POST`), `qualityProfile`.
- Raw HTTP: `GET {base_url}/api/v3/customformat` with header `X-Api-Key`.

Config keys read: `sonarr_instances` (then per-instance `base_url`, `api`).

global_cache keys:
- Read/generate: `CacheKeyPaths.sonarr.CUSTOM_FORMATS` = `sonarr/<instance>/custom_formats` (via `get_or_generate_cache`).
- Write: `key_builder.format_cache_key("sonarr", instance_name, "custom_formats")` — payload `{"customFormats": [...], "meta": {timestamp, instance, count}}` via `set_with_pretty_output`.

dry_run: `self.dry_run` is captured (kwarg → parent `manager.dry_run` → False) but is **not consulted** in `add_custom_format` / `sync_custom_formats`. As written, the `POST` happens regardless of dry_run — flagging this as a dry_run gap (the project's "would ..." convention is not honored here).

Singleton / concurrency: standard `BaseManager` singleton; no threading.

## How it functions

Init mirrors the other quality submanagers: `BaseManager.__init__` + `register()`, then pull `sonarr_api`, `logger`, `manager`, `instance_manager`, `key_builder`, `dry_run` from kwargs or registry parent; raise if no logger; debug "Initialized" line.

There is no single `run()`. Typical flows:
- Read path: `get_custom_formats` / `get_custom_format_scores` / `get_profile_scores_by_format`, each resolving the instance first.
- Write path: `sync_custom_formats` → fetch existing → `_deduplicate_custom_formats` → per new CF, `_custom_format_exists` guard → `add_custom_format` (`POST`).
- Bulk cache path: `run_custom_format_data_pull` iterates `sonarr_api.get_all_sonarr_apis()` and caches each instance's raw custom-format list.

No `machine_learning` brain module is invoked here. (Custom-format *scores* are simply read from Sonarr; any decision that consumes them lives in the selector submanager / brain, not here.)

## Criteria & examples

- **Duplicate guard (`_custom_format_exists`, used by `sync_custom_formats`):** name match is case-insensitive. Example: syncing a new CF named `"x265"` when the instance already has `"X265"` → treated as existing → skipped, `skipped_count += 1`. A genuinely new CF named `"DV HDR10+"` → `add_custom_format` is called and `synced_count += 1`.
- **Profile score aggregation (`get_profile_scores_by_format`):** only `formatItems` with `enabled == True` and a non-None `format.id` contribute, and scores for the same `format_id` across profiles are summed. Example: format id 7 enabled with score 50 in "1080p" and score 25 in "4K" → `{7: 75}`.
- **Unused detection (`log_unused_custom_formats`):** a CF whose `id` does not appear among the keys of `get_profile_scores_by_format` is logged as "Unused CF". (Note: `get_profile_scores_by_format` keys are *format ids* gated on `enabled`, so a CF only enabled-with-score in some profile counts as used.)

## In plain English

In Sonarr, a "Custom Format" is a saved preference like "I prefer x265 video" or "avoid releases with burned-in subtitles," each worth points. This component is the librarian for that rulebook. It can read the current rules and their point values, add a new rule, and copy a set of rules onto a server that's missing them — but it's careful not to add a duplicate rule that's already there (even if the spelling differs only by capitalization). It can also point out rules nobody is actually using, so the rulebook stays tidy. Think of it as keeping the "what counts as a good copy of *The Office*" preferences consistent everywhere.

## Interactions

- **Parent manager:** `SonarrQualityManager`.
- **Sibling submanagers:** `SonarrQualityAdjustmentManager`, `SonarrQualityFileSizesManager`, `SonarrQualitySelectorManager`.
- **Services it talks to:** the Sonarr API adapter (`sonarr_api`) for `customformat`/`qualityProfile` reads and the `POST`; `instance_manager` for resolution; `global_cache` for the custom-formats cache; `key_builder` for cache-key formatting.
- **Brain modules:** none directly. The selector submanager consumes custom-format scores when delegating quality-profile choice to `ml_manager`.
