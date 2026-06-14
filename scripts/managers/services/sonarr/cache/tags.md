# SonarrCacheTagManager

- **One-liner** — Caches Sonarr tags per instance and supports refreshing, syncing tags across instances, adding/removing tags in the cache, and resolving the `"keep"` tag's IDs.
- **File** — `scripts/managers/services/sonarr/cache/tags.py`

## What it does (for a senior Python engineer)

`SonarrCacheTagManager(BaseManager, ComponentManagerMixin)` is reachable as `sonarr_cache.tags`.

Public methods:
- `format_tag_cache_key(instance)` — returns `f"sonarr/{instance}/tags.json"` (the single cache key used throughout).
- `refresh_tag_cache(instance)` — FETCH `self.sonarr_api.get_tags(instance)`, CACHE to `sonarr/{instance}/tags.json`.
- `get_tags(instance)` — read that key back (`[]` default).
- `sync_tags_across_instances(source_instance, target_instances)` — read source tags, then for each target APPLY `self.sonarr_api.update_tags(target, source_tags)` and CACHE the same list under the target's key.
- `add_tag_to_cache(instance, tag_name)` / `remove_tag_from_cache(instance, tag_name)` — local cache-only mutations (append/remove a value if (not) present, then re-cache). These do **not** call Sonarr.
- `get_keep_tag_ids(instance)` — return the integer `id`s of tags whose `label` (lower-cased) equals `"keep"`.

FETCH / CACHE / APPLY: FETCH (`get_tags`), CACHE (`tags.json`), and APPLY only in `sync_tags_across_instances` (`update_tags`). External API: `self.sonarr_api.get_tags`, `self.sonarr_api.update_tags`. Config keys: none. Cache key: `sonarr/{instance}/tags.json` (read/write).

`dry_run`: captured in `__init__`; not checked here, so `sync_tags_across_instances` would issue `update_tags` regardless of `dry_run`. The local add/remove helpers are non-destructive cache edits.

Note: `get_keep_tag_ids` assumes tag dicts with `label`/`id` (Sonarr's tag shape), whereas `add_tag_to_cache`/`remove_tag_from_cache` treat the cached list as a list of plain strings. The two access patterns are not symmetric — `get_keep_tag_ids` is the one that matters to the `"keep"`-policy guards downstream.

## How it functions

Init derives `parent_name` from the class name (`"SonarrCacheTag"`), wires the dual cache + `sonarr_api`/`logger`/`manager`/`dry_run`, registers, raises without a logger. No `load_components` (no submanagers). Each method is a small cache read/write or a per-target API+cache loop. No decision is delegated to a `machine_learning` module.

## Criteria & examples

- `get_keep_tag_ids`: a cached tag `{"id": 3, "label": "Keep"}` is matched (case-insensitive) and `3` is returned; `{"id": 4, "label": "anime"}` is not. These IDs are what the episode-files keep-policy resolution maps to `keep_series` / `keep_season`.
- `sync_tags_across_instances("default", ["4k"])`: copies `default`'s tag list to the `4k` Sonarr instance and mirrors it into `sonarr/4k/tags.json`.

## In plain English

Tags are the colored stickers you put on shows — like a "keep forever" sticker on Bluey so it never gets cleaned up. This clerk keeps a copy of all your stickers for quick reference, can copy your sticker set from one Sonarr server to another, and can quickly tell the rest of the system exactly which sticker means "keep" so the cleanup crew knows what is off-limits.

## Interactions

- **Parent manager:** `SonarrCacheManager` (attached as `tags`).
- **Services:** the `sonarr_api` gateway (`SonarrInstanceManager`) for `get_tags` / `update_tags`; `global_cache` for the tags JSON cache.
- **Consumers:** the keep-policy logic in `SonarrCacheEpisodeFilesManager` ultimately depends on the `"keep"` tag (resolved against Sonarr series tags) to set `keep_series` / `keep_season` deletion exemptions.
- **Brain modules:** none directly.
