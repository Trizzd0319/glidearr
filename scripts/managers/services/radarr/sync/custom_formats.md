# RadarrSyncCustomFormatsManager

**File** — `scripts/managers/services/radarr/sync/custom_formats.py`
**One-liner** — Reads, deduplicates, and (when permitted) propagates Radarr "custom formats" across instances, using name-similarity and regex-content heuristics to avoid creating near-duplicate formats.

## What it does (for a senior Python engineer)

`RadarrSyncCustomFormatsManager(BaseManager, ComponentManagerMixin)` is a leaf service manager under `RadarrSyncManager`. It performs FETCH (GET customformat / qualityprofile), CACHE (per-instance custom-format list), and APPLY (POST a custom format). No `load_components`; no submanagers.

Public methods:
- `deduplicate_custom_formats(custom_formats) -> list` — collapses exact duplicates by `json.dumps(..., sort_keys=True)` serialization (falls back to `str(obj)` on `TypeError`).
- `custom_format_exists(new_cf, existing_formats) -> bool` — fuzzy "do we already have this?" check: `True` if any existing format's `name` is ≥0.85 similar (`difflib.SequenceMatcher` ratio, case-insensitive) **or** if their regex specification values conflict.
- `get_custom_formats(instance) -> list` — FETCH + CACHE. Served from `global_cache` key `radarr.custom_formats.<resolved>` if present, else GET `customformat` and cache it.
- `add_custom_format(instance, custom_format_payload)` — APPLY. POSTs `customformat`; under `dry_run` logs a "would add" line and returns `None`.
- `get_custom_format_scores(instance) -> dict` — FETCH. GETs `customformat` (not cached) and returns `{name: score}` (`score` defaulting to 0).
- `sync_all_custom_formats()` — orchestration. Builds a deduplicated master set across all configured instances and pushes any format an instance lacks. Returns early if no instances configured.
- `get_profile_scores_by_format(instance) -> dict` — FETCH. GETs `qualityprofile`, sums each profile's `formatItems` scores per format ID (ignoring score==0), returns `{format_id: total_score}`. Handles `format` being either a scalar id or a `{"id": ...}` dict.

Helpers:
- `_resolve_instance(instance)` — `instance_manager.resolve_instance` → `radarr_api.resolve_instance` → `instance or "default"`.
- `_regex_content_conflict(cf1, cf2) -> bool` — extracts all `specifications[].fields[].value` strings from each format, compiles each value of `cf1` as a case-insensitive regex, and returns `True` if any `cf1` pattern `.search`-matches any `cf2` value. Swallows `re.error`/`TypeError`.

External API endpoints (via `radarr_api._make_request`): `customformat` (GET, POST), `qualityprofile` (GET).
Config keys read: `radarr_instances` (a `{name: ...}` map — `sync_all_custom_formats` iterates its keys).
global_cache keys: `radarr.custom_formats.<resolved_instance>` (read/written in `get_custom_formats`; **not** used by `sync_all_custom_formats`, which fetches raw each time, nor invalidated after a POST).
dry_run: gates the POST in `add_custom_format`.
Concurrency/singleton: standard `BaseManager` singleton; no threading.

## How it functions

Lifecycle: `__init__` injects shared deps, sets `parent_name="RadarrSyncManager"`, calls `register()`, captures `radarr_api`/`instance_manager`/`dry_run`.

`sync_all_custom_formats` control flow: read `config["radarr_instances"]` keys → per instance GET `customformat` and `deduplicate_custom_formats` it into `instance_cf_map[resolved]` → fold those into a single `all_unique_cfs` master list, admitting a format only if `custom_format_exists(cf, all_unique_cfs)` is `False` → for each instance, `add_custom_format` any master format the instance is judged not to already have (again via `custom_format_exists`). So the same fuzzy predicate guards both master-set construction and per-instance push.

No decision is delegated to a `machine_learning` brain module; the dedup/similarity logic is local and rule-based.

## Criteria & examples

- **Exact dedup.** Two identical format dicts (same keys/values) serialize to the same sorted-JSON string, so only the first survives `deduplicate_custom_formats`.
- **Name-similarity guard (0.85).** A new format named `"x265 (HEVC)"` vs existing `"x265 HEVC"` — `SequenceMatcher` ratio ≥ 0.85 → treated as already existing, not added. A format named `"DV HDR10"` vs `"Remux Tier 01"` scores well below 0.85, so it would be admitted (unless a regex conflict triggers).
- **Regex-content conflict.** If existing format has a spec field value `\bAMZN\b` and the new format has a field value `Amazon.WEB.AMZN.1080p`, the case-insensitive pattern `\bAMZN\b` matches → `custom_format_exists` returns `True`, suppressing the duplicate even though the names differ.
- **Profile score aggregation.** A format id `7` scored `+25` in profile "HD" and `+25` in profile "UHD" yields `{7: 50}` from `get_profile_scores_by_format`; a format scored `0` is skipped.

## In plain English

"Custom formats" are Radarr's rules for recognizing release flavors — e.g. "this is a Dolby Vision copy" or "this came from Amazon." Different storerooms (instances) may have built up slightly different, overlapping rule sheets. This manager gathers every storeroom's rules, throws out exact copies, and is smart enough to notice when two rules are basically the same idea worded differently (a name that's 85%+ alike, or a pattern that would catch the same releases). It then makes sure every storeroom ends up with the full combined set — so whether you grab The Avengers from storeroom A or B, the same quality-recognition rules apply. In pretend mode it just narrates what it would copy over.

## Interactions

- **Parent:** `RadarrSyncManager`.
- **Siblings:** `RadarrSyncFoldersManager`, `RadarrSyncMediaManager`, `RadarrSyncNamingManager`, `RadarrSyncTagsManager`.
- **Services:** `radarr_api` (`RadarrInstanceManager`) for HTTP; `instance_manager` for resolution; `global_cache` for the per-instance custom-format cache; reads `radarr_instances` config.
- **Brain modules:** none.
