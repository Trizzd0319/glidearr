# RadarrTagCacheManager

- **File** — `scripts/managers/services/radarr/cache/tags.py`
- **One-liner** — Caches each Radarr instance's tag list and provides simple read/sync/add/remove helpers over that cache.

## What it does (for a senior Python engineer)

`RadarrTagCacheManager(BaseManager, ComponentManagerMixin)` is a thin tag adapter. It performs FETCH (GET `tag`), CACHE (writes `radarr.tags.<instance>`), and one APPLY path (PUT `tag` during cross-instance sync).

Where it sits in the tree:
- **Parent**: `RadarrCacheManager` (`parent_name = "RadarrCacheManager"`).
- **Submanagers**: none (loads no components).

Public methods:
- `refresh_tag_cache(instance)` — FETCH `GET tag` via `radarr_api._make_request(instance, "tag", fallback=[])`; on a non-empty result, CACHE under `radarr.tags.<instance>` (`compressed=True`). Logs a count or a warning.
- `get_tags(instance)` — returns cached `radarr.tags.<instance>` (default `[]`).
- `sync_tags_across_instances(source_instance, target_instances)` — reads source tags, then for each target does APPLY `PUT tag` with the source tag payload and overwrites the target's cached `radarr.tags.<target>`. Per-target try/except logs failures.
- `add_tag_to_cache(instance, tag_name)` — appends `{"id": None, "label": tag_name}` to the cached list if no tag with that label exists, and rewrites the cache. Cache-only (no API call).
- `remove_tag_from_cache(instance, tag_name)` — removes any tag whose `label` matches and rewrites the cache if something changed. Cache-only.
- `get_keep_tag_ids(instance)` — returns the `id`s of tags whose label is `"keep"` (case-insensitive).

External API endpoints: `GET tag`, `PUT tag` (via `radarr_api._make_request`).
Config keys read: none.
Global_cache keys: reads/writes `radarr.tags.<instance>`.

`dry_run`: captured into `self.dry_run` but NOT consulted — `sync_tags_across_instances` will issue a `PUT` even under dry_run. This is a divergence from the FETCH/CACHE/APPLY dry-run convention (the APPLY here is not gated).

Singleton/concurrency: standard `BaseManager` singleton; no threading of its own.

## How it functions

`__init__` does the BaseManager wiring, `self.register()`, then resolves `radarr_api`, `instance_manager`, `manager` (parent), and `dry_run` from kwargs-or-parent. There is no `run()` entry point and no `load_components` call — callers invoke the individual helpers directly. No machine_learning delegation.

## Criteria & examples

- `add_tag_to_cache` guard: it only appends when the label is absent. Example: cache holds `[{"id": 3, "label": "keep"}]`; calling `add_tag_to_cache(inst, "keep")` does nothing, while `add_tag_to_cache(inst, "anime")` appends `{"id": None, "label": "anime"}` (note `id` is `None` until a real refresh re-fetches IDs from Radarr).
- `get_keep_tag_ids` match is case-insensitive on the exact word "keep": a tag labelled `"Keep"` returns its id; a tag labelled `"keep-forever"` does NOT (it is not equal to `"keep"`).

## In plain English

Radarr lets you stick coloured sticky-labels on movies — like a "keep" label on The Princess Bride so it never gets cleaned up. This manager keeps a local photocopy of every label so the rest of the system can check "does this movie say keep?" without phoning Radarr each time. It can also copy one server's set of labels onto another server, and it can look up exactly which label means "keep".

## Interactions

- **Parent**: `RadarrCacheManager`.
- **Siblings**: shares the cached `radarr.tags.<instance>` data that `RadarrCacheMovieFilesManager.refresh` reads (to build `tag_labels`) and that `RadarrMonitoringCacheManager.enforce_keep_tags` conceptually relies on.
- **Services**: `radarr_api` (the Radarr instance API) for the `tag` GET/PUT.
- **Brain modules**: none.
