# SonarrSeriesRetrievalValidationManager

- **File** — `scripts/managers/services/sonarr/series/retrieval/validate.py`
- **One-liner** — Integrity checker for the cached Sonarr series library: it flags count drift between live and cached series, missing required schema fields, and tag references that point at non-existent tags.

## What it does (for a senior Python engineer)

`SonarrSeriesRetrievalValidationManager(BaseManager, ComponentManagerMixin)` runs read-only consistency checks against the letter-bucketed series cache and reports problems via logging and return values. It never mutates the cache or Sonarr.

**Position in the manager tree**
- Loaded by `SonarrSeriesRetrievalManager` as the `validate` component.
- `parent_name` is derived as `self.__class__.__name__.replace("Manager", "")` → `"SonarrSeriesRetrievalValidation"`.
- Deps lifted off `kwargs["manager"]`: `sonarr_cache`, `global_cache`, `sonarr_api`, `logger`, `instance_manager`.
- `self.series_cache` prefers `sonarr_cache.series` (the canonical `SonarrCacheSeriesManager`) and falls back to `manager.series_cache` (the retrieval-layer facade) if the cache layer isn't wired yet.
- Hard requirement: raises `ValueError` in `__init__` if no logger can be resolved.

**FETCH / CACHE / APPLY** — FETCH for the live count (`validate_series_count` hits Sonarr) plus cache reads; otherwise pure cache-read validation. No CACHE writes, no APPLY.

**Public methods**
- `validate_series_count(instance) -> float` — compares the live Sonarr series count against the cached id count and returns the relative difference. Live list comes from `sonarr_api.get_all_sonarr_apis()[resolved_instance].all_series()`; cached count from `series_cache.get_all_series_ids(resolved_instance)`. Computes `diff_pct = abs(live - cache) / max(live, 1)`; warns if `> 0.10`, else logs pass. Returns `diff_pct`.
- `validate_series_schema(instance, required_fields=None) -> list` — for every series in every letter bucket, checks each required field is truthy. Default required fields: `["id", "title", "path", "qualityProfileId"]`. Returns a list of `{"id", "title", "missing"}` error dicts (logs up to the first 10).
- `validate_series_tags(instance) -> list` — builds the known-tag id set from the cache key `sonarr/{instance}/tags.json` (via `sonarr_cache.get(...)`), then scans every cached series' `tags` for ids not in that set. Returns a list of `(series_id, tag_id)` invalid usages (logs up to the first 10).

**Config keys** — none read directly.
**Cache keys** — reads letter buckets via `series_cache.load_letter_cache(instance, letter)` and the tag list via `sonarr_cache.get("sonarr/{instance}/tags.json")`.
**dry_run** — not applicable (read-only).
**Concurrency** — none; synchronous scans over the 37 letter buckets.

## How it functions

Lifecycle: standard `BaseManager` init, dep resolution, logger assertion, then on-demand validation calls (no `run`/`prepare` method — it is invoked by callers that want a specific check).

Each validator resolves the instance through `instance_manager.resolve_instance`, then iterates the canonical bucket alphabet `"abcdefghijklmnopqrstuvwxyz0123456789_"`. `validate_series_count` is the only one that also makes a live API call (through the arrapi object's `all_series()`).

No `machine_learning` brain module is involved — these are mechanical integrity checks, not value judgements.

## Criteria & examples

- **Count drift threshold = 10%.** Example: live = 1,000, cached = 880 → `diff_pct = 120 / 1000 = 0.12` → `> 0.10` → warning logged. Live = 1,000, cached = 950 → `0.05` → pass.
- **Schema required fields** default to `id`, `title`, `path`, `qualityProfileId`; any falsy/missing value is an error. Example: a cached series with `path: ""` and no `qualityProfileId` produces `{"id": 42, "title": "Foo", "missing": ["path", "qualityProfileId"]}`.
- **Tag validity:** a series tagging `[3, 99]` when `tags.json` only knows ids `{1, 2, 3}` yields one invalid usage `(series_id, 99)`.

## In plain English

This is the auditor who periodically checks the library's records are honest. They do three spot-checks: (1) does the number of show cards in our drawers roughly match what the warehouse says it actually has? If they're off by more than 10%, raise a flag. (2) Does every card have the must-have fields filled in — title, location on the shelf, quality? (3) Does every label stuck on a card actually correspond to a real label in our label book, or did someone stick on a label code that doesn't exist? The auditor only reports problems; they never change anything themselves.

## Interactions

- **Parent manager:** `SonarrSeriesRetrievalManager`.
- **Siblings:** reads the same letter-bucketed cache the `fetch`/`cache` managers use (`sonarr_cache.series`).
- **Services:** `sonarr_api` (live count), `instance_manager` (resolve instance), `sonarr_cache` (tag list + bucket cache).
- **Brain modules:** none.
