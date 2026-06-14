# RadarrCustomFormatsManager

**File** — `scripts/managers/services/radarr/quality/custom_formats.py`
**One-liner** — Adapter over Radarr's `customformat`/`qualityprofile` endpoints that fetches, caches, adds, deduplicates, and audits custom formats and their per-profile scores.

## What it does (for a senior Python engineer)

`RadarrCustomFormatsManager(BaseManager, ComponentManagerMixin)` is a thin service adapter for Radarr **custom formats** (named release-attribute matchers, e.g. "Remux", "x265", "HDR") and the scores profiles assign to them. It performs FETCH (GET customformat / qualityprofile), CACHE (store the customformat list), and APPLY (POST a new custom format).

Public methods:
- `get_custom_formats(instance) -> list` — CACHE-first FETCH. Returns cached `radarr.custom_formats.{resolved}` if present; otherwise GETs `customformat`, caches it, returns it.
- `add_custom_format(instance, custom_format_payload) -> dict|None` — APPLY. POSTs `customformat`. **dry_run aware**: when `self.dry_run` is True it logs `[dry_run] Would add custom format '<name>'` and returns `None` without calling the API.
- `get_custom_format_scores(instance) -> dict` — FETCH (always live, no cache). GETs `customformat`, returns `{name: score}` (default score 0).
- `sync_custom_formats(instance, new_custom_formats) -> None` — APPLY loop. Fetches + dedupes existing formats, then for each entry in `new_custom_formats` adds it only if a same-name (case-insensitive) format doesn't already exist; logs an Added/Skipped tally. Honors dry_run indirectly through `add_custom_format`.
- `get_profile_scores_by_format(instance) -> dict` — FETCH + aggregate. GETs `qualityprofile`, walks each profile's `formatItems`, and sums non-zero scores per format id into `{format_id: total_score}`. Handles `format` being either an id or a nested `{"id": ...}` dict.
- `log_unused_custom_formats(instance) -> None` — Diagnostic. GETs all custom formats, computes the set of used format ids via `get_profile_scores_by_format`, and logs which formats are referenced by no profile.

Internal helpers:
- `_resolve_instance(instance)` — standard instance resolution.
- `_custom_format_exists(name, formats)` — case-insensitive name membership test.
- `_deduplicate_custom_formats(formats)` — drops case-insensitive duplicate names, preserving first occurrence.

- **Parent manager**: `RadarrQualityManager`.
- **Submanagers loaded**: none.
- **External API endpoints**: `GET customformat`, `POST customformat`, `GET qualityprofile`.
- **config keys read**: none.
- **global_cache keys**: reads/writes `radarr.custom_formats.{resolved}`.
- **dry_run**: enforced in `add_custom_format` (and therefore in `sync_custom_formats`).
- **Singleton / concurrency**: standard `BaseManager` singleton; no threading.

## How it functions

`__init__` does standard wiring (super, register, resolve radarr_api/instance_manager/dry_run, debug log). No `load_components`, no top-level `run()`. The methods are invoked ad hoc to read or push custom-format configuration. Caching is only applied to the customformat list via `get_custom_formats`; `get_custom_format_scores`, `sync_custom_formats`, and the audits read live each time. No `machine_learning` brain module is involved — there are no value judgements here, just read/dedupe/write of release-matching rules.

## Criteria & examples

- **Skip-if-exists (sync)**: a case-insensitive name match short-circuits the add. Example: syncing a payload named `"REMUX"` when a `"Remux"` already exists → skipped (`skipped_count += 1`), no POST.
- **Score aggregation**: in `get_profile_scores_by_format`, only `score != 0` entries are summed. Example: across three profiles a format id `12` scored `+50`, `0`, `+50` → result `{12: 100}` (the zero is ignored, and a format scoring only 0 everywhere never appears).
- **Unused detection**: a custom format with id `34` that no profile references → reported as `Unused CF: '<name>' (ID: 34)`.

## In plain English

Custom formats are like sticky-note labels Radarr puts on a download to say "this one is a Remux", "this one is HDR", "this one has subtitles you don't want". Each quality profile gives those labels a thumbs-up or thumbs-down score so Radarr prefers the right kind of file. This manager keeps the master list of those labels, can add new ones (but won't add a duplicate), keeps a quick-reference copy, and can point out labels nobody is actually using — like noticing you printed a "Director's Cut" sticker but never put it on any shelf. In dry-run it only says "I would add this label" without actually doing it.

## Interactions

- **Parent**: `RadarrQualityManager`.
- **Siblings**: `RadarrQualityAdjustmentManager`, `RadarrFileSizesManager`, `RadarrQualitySelectorManager`, `RadarrSpacePressureManager`, `RadarrQualityUniverseManager`. (The selector reads custom-format-derived scores; this manager produces the score aggregation primitive.)
- **Services**: `radarr_api`, `instance_manager`/`radarr_api.resolve_instance`, `global_cache`.
- **Brain modules**: none.
