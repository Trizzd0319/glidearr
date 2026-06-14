# SonarrSyncCustomFormatsManager

- **File** — `scripts/managers/services/sonarr/sync/custom_formats.py`
- **One-liner** — Collects the union of "custom formats" (quality-matching regex rule sets) across all Sonarr instances and pushes any missing ones to each instance, deduplicating by content and fuzzy name/regex similarity.

## What it does (for a senior Python engineer)

`SonarrSyncCustomFormatsManager(BaseManager, ComponentManagerMixin)` reconciles Sonarr "custom format" definitions so every configured instance ends up with the same superset. A custom format is a Sonarr object with a `name` and a list of `specifications`, each holding `fields` with regex `value`s.

Position in the manager tree:
- **Parent** — resolved at runtime from the class name: `parent_name` is set to the class name minus the `"Manager"` suffix, i.e. `"SonarrSyncCustomFormats"`. (The literal default `"SonarrStorage"` assigned first is immediately overwritten.) It then looks up `self.registry.get("manager", self.parent_name)` and falls back to the parent's `sonarr_api` / `logger` / `manager` if those weren't injected.
- **Submanagers** — none (leaf).

FETCH / CACHE / APPLY:
- FETCH — `self.sonarr_api._make_request(instance, "customformat")` (HTTP GET of `customformat`).
- APPLY — `self.sonarr_api.add_custom_format(instance, cf)` for each format missing on an instance.
- CACHE — none.

External API endpoints touched: Sonarr `customformat` (GET via `_make_request`; create via the `add_custom_format` helper).

Config keys read: `self.config.get_sonarr_instances()` (the list of instance names).

global_cache / Parquet keys: none read or written.

dry_run behavior: `self.dry_run` is captured in `__init__`, but **`sync_all_custom_formats` does not consult it** — the APPLY path calls `add_custom_format` unconditionally. (If dry-run suppression is expected here, it is not implemented in this file.)

Singleton / concurrency: BaseManager singleton. Note the regex-conflict check spins up a short-lived `ThreadPoolExecutor(max_workers=1)` per regex pair to enforce a per-match timeout (`_REGEX_MATCH_TIMEOUT = 2.0s`); regexes longer than `_REGEX_MAX_LEN = 500` chars are skipped outright. This is a ReDoS guard.

Public methods:
- `deduplicate_custom_formats(custom_formats)` — drops exact duplicates by `json.dumps(obj, sort_keys=True)` (falls back to `str(obj)` on `TypeError`). Returns the unique list, order-preserving.
- `custom_format_exists(new_cf, existing_formats)` → bool — True if any existing format is a fuzzy name match (`SequenceMatcher` ratio ≥ 0.85, case-insensitive) **or** has a conflicting regex.
- `regex_content_conflict(cf1, cf2)` → bool — compiles each regex from `cf1`'s spec fields (IGNORECASE) and searches it against each regex string from `cf2`; returns True on any match. Compile errors, oversize patterns, and match timeouts are skipped/logged, not raised.
- `sync_all_custom_formats()` — the entry point; see flow below.

## How it functions

`__init__` does standard BaseManager wiring, `register()`, resolves parent + injected deps, and raises `ValueError` if no logger could be resolved.

`sync_all_custom_formats()` control flow:
1. Get instance names via `config.get_sonarr_instances()`.
2. Build `instance_cf_map`: for each instance, FETCH its `customformat` list and `deduplicate_custom_formats` it.
3. Build `all_unique_cfs` by walking every instance's formats and appending only those not already present (per `custom_format_exists`) — the cross-instance superset.
4. For each instance, for each format in the superset, if `custom_format_exists(cf, existing_cfs)` is False, APPLY via `add_custom_format` and log ✅/❌ based on the truthy result.

Internal helpers: the nested `sorted_json` (stable serialization) and `is_similar` (fuzzy name match) closures.

Brain delegation: none.

## Criteria & examples

- **Name similarity ≥ 0.85** treats two formats as the same. Example: an existing format `"x265 (HD)"` vs a new `"x265 HD"` — `SequenceMatcher` ratio is well above 0.85, so the new one is considered already present and is **not** added.
- **Regex conflict** also counts as "exists." Example: existing format carries regex `1080p` and a new format's field is the literal string `"1080p Remux"` — `re.compile("1080p").search("1080p Remux")` matches, so the new format is treated as a duplicate and skipped.
- **ReDoS guard** — a pathological regex like `(a+)+$` evaluated against a long string would normally hang; the per-pair `ThreadPoolExecutor` cancels it after 2.0s, logs `⚠️ Regex match timed out`, and moves on. Any regex string longer than 500 chars is skipped before compilation.
- **Exact dedupe** — two byte-identical format dicts on the same instance collapse to one via `sorted_json` before any similarity work.

## In plain English

Imagine three different DVRs that each have a list of "rules" for picking the best version of a show — e.g. "prefer 4K," "avoid the dubbed cut," "skip the tiny low-quality file." Over time the three DVRs drift apart: one learned a rule the others never got. This manager gathers every rule from all three, throws out near-identical ones (so you don't end up with "prefer 4K" twice under slightly different names), and copies the missing rules onto whichever DVR lacks them — so all three end up making the same smart choices about which file of, say, *The Mandalorian* to keep.

## Interactions

- **Parent** — `SonarrSyncManager` (registered as `SonarrSyncCustomFormats`).
- **Sibling submanagers** — `SonarrSyncFoldersManager`, `SonarrSyncMediaManager`, `SonarrSyncNamingManager`, `SonarrSyncTagsManager`.
- **Services** — the Sonarr API client (`sonarr_api`) for the `customformat` GET and `add_custom_format` create.
- **Brain modules** — none.
