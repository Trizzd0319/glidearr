# RadarrSyncTagsManager

**File** — `scripts/managers/services/radarr/sync/tags.py`
**One-liner** — Reads, builds, and (when permitted) creates Radarr tag definitions, and keeps the set of tag labels consistent across every Radarr instance — plus tracks which movies carry a "keep" tag.

## What it does (for a senior Python engineer)

`RadarrSyncTagsManager(BaseManager, ComponentManagerMixin)` is a leaf service manager under `RadarrSyncManager`. It performs FETCH (GET movies/tags), CACHE (per-instance tag list in `global_cache`), and APPLY (POST a new tag). It does **not** call `load_components`; it has no submanagers.

State held on the instance:
- `keep_tagged_movies: set` — movie IDs tagged `"keep"` or `"keep_forever"`.
- `global_tag_map: dict` — `{movie_id: [tag_ids]}` built across all instances.
- `master_tag_set: set` — union of all tag IDs seen.

Public methods:
- `refresh_tags_across_instances()` — FETCH. For every instance, GETs `movie` and walks each movie's `tags`, populating `global_tag_map`, `master_tag_set`, and `keep_tagged_movies`. No return; mutates instance state.
- `is_movie_tagged_keep(movie_id) -> bool` — membership test against `keep_tagged_movies`.
- `get_movies_with_tag(tag) -> list` — returns movie IDs whose tag list contains `tag` (note: `global_tag_map` stores tag *IDs*, so callers must pass an ID, not a label).
- `get_tag_labels(instance) -> list` — FETCH + CACHE. Returns the instance's full tag definition list (`[{id, label}, ...]`), served from `global_cache` key `radarr.tags.<resolved>` if present, else GET `tag` and cache it.
- `ensure_tag_exists(instance, label) -> int` — APPLY. Looks up the label (case-insensitive) in the cached tag list; returns its `id` if found. Otherwise POSTs `tag` with `{"label": label}`, invalidates the cache, and returns the new ID. Under `dry_run` it logs a "would create" line and returns `-1`. On failed POST returns `-1`.
- `sync_tags_across_instances()` — orchestration. Collects the union of all tag labels across every instance, then for each instance creates any label it is missing via `ensure_tag_exists`. Logs and returns early if no instances exist.

Helpers:
- `_resolve_instance(instance)` — resolves an instance handle via `instance_manager.resolve_instance` → `radarr_api.resolve_instance` → `instance or "default"`.
- `_get_all_instances()` — keys of `radarr_api.get_all_radarr_apis()`; `[]` on failure.

External API endpoints touched (via `radarr_api._make_request`): `movie` (GET), `tag` (GET, POST).
Config keys read: none directly (instances come from `radarr_api`).
global_cache keys: `radarr.tags.<resolved_instance>` (read in `get_tag_labels`; written there; invalidated/set to `None` after a successful POST in `ensure_tag_exists`).
dry_run: gates the POST in `ensure_tag_exists` only.
Concurrency/singleton: standard `BaseManager` singleton; no threading.

## How it functions

Lifecycle: `__init__` injects shared deps, sets `parent_name="RadarrSyncManager"`, calls `register()`, captures `radarr_api`/`instance_manager`/`dry_run`, and initializes the three state containers.

Main flow for a full sync: `sync_tags_across_instances()` gathers the global label union (raw GET `tag` per instance), then per instance compares lowercased existing labels to the union and calls `ensure_tag_exists` for each gap — which in turn may POST and invalidate the cache. `refresh_tags_across_instances()` is the read-side counterpart that rebuilds the keep-set and tag map from live movie data.

No decision is delegated to a `machine_learning` brain module. Note that the "keep" / "keep_forever" tags this manager surfaces are consumed elsewhere as protection signals (e.g. by deletion guards), but this manager only reads/reports them.

## Criteria & examples

- **Keep detection.** A movie whose `tags` array contains `"keep"` *or* `"keep_forever"` is added to `keep_tagged_movies`. Example: movie id `4527` with `tags: ["keep", "1080p"]` → `is_movie_tagged_keep(4527)` returns `True`.
- **Label match in `ensure_tag_exists`.** Comparison is case-insensitive: if the instance already has a tag labelled `"Keep"`, calling `ensure_tag_exists(inst, "keep")` returns the existing ID and POSTs nothing.
- **Sync gap.** If instance A has labels `{keep, 4k}` and instance B has `{keep}`, the union is `{keep, 4k}`; the sync creates `4k` on B (or logs `[dry_run] Would create tag '4k'` and returns `-1` under dry_run).

## In plain English

Imagine you run several DVD shelves (Radarr instances) and you use colored stickers to mark discs — a gold "keep" sticker means "never throw this out." This manager walks every shelf, notes which discs wear a gold sticker, and makes sure all your shelves stock the *same set* of sticker types. If shelf B is missing the "4K" sticker that shelf A uses, it prints a new sheet of those stickers for shelf B — unless you're in "just pretend" mode, in which case it only says what it *would* print. So later, when something decides whether to discard a movie like The Princess Bride, it can check: does this disc have the gold "keep" sticker?

## Interactions

- **Parent:** `RadarrSyncManager`.
- **Siblings:** `RadarrSyncCustomFormatsManager`, `RadarrSyncFoldersManager`, `RadarrSyncMediaManager`, `RadarrSyncNamingManager`.
- **Services:** `radarr_api` (`RadarrInstanceManager`) for all HTTP; `instance_manager` for instance resolution; `global_cache` for the per-instance tag cache.
- **Brain modules:** none. (The keep-tag set it produces is consumed by downstream deletion-guard logic, not decided here.)
