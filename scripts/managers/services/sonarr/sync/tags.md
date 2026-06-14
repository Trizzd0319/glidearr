# SonarrSyncTagsManager

- **File** — `scripts/managers/services/sonarr/sync/tags.py`
- **One-liner** — Collects and reconciles Sonarr series tags across all instances, maintains the "keep"-tagged series set used to protect shows from deletion, and caches per-instance tag tables.

## What it does (for a senior Python engineer)

`SonarrSyncTagsManager(BaseManager, ComponentManagerMixin)` is the tag authority for the Sonarr service. It builds a unified view of which series carry which tags, computes the set of series tagged `keep` (a deletion-protection signal), can push the union of all tags onto every series on every instance, and caches each instance's tag id/label table.

Position in the manager tree:
- **Parent** — resolved from the class name: `parent_name` becomes `"SonarrSyncTags"` (class name minus `"Manager"`; the literal `"SonarrStorage"` default is overwritten). Falls back to the parent's `sonarr_api` / `logger` / `manager` / `instance_manager` if not injected.
- **Submanagers** — none (leaf).

FETCH / CACHE / APPLY:
- FETCH — series and tags via the instance API clients (`api.get_all_series()`, `sonarr_api._make_request(inst, "tag")`) and a direct `requests.get(<base_url>/api/v3/tag)` in `run_tag_data_pull`.
- CACHE — per-instance tag table under key `sonarr.<instance>.tags` (built by `CacheKeyBuilder.format_cache_key("sonarr", instance_name, "tags")`) via `global_cache.set_with_pretty_output`.
- APPLY — `api.update_series_tags(series_id, updated_tags)` in `sync_tags_across_instances`.

External API endpoints touched: Sonarr `tag` (GET via `_make_request` and via raw `requests.get`); series via the API client's `get_all_series` / `update_series_tags` helpers.

Config keys read:
- `self.config.get("sonarr_instances", {})` — per-instance config in `run_tag_data_pull`, from which it reads `base_url` and `api` (the API key).

global_cache / Parquet keys:
- Writes `sonarr.<instance>.tags` (a `{tags: [...], meta: {...}}` blob) in `run_tag_data_pull`.
- Reads cached keep-tag ids indirectly via the `SonarrCacheTagManager`'s `get_keep_tag_ids` and series via `SonarrCacheSeriesManager.iter_all_series`.

dry_run behavior: `self.dry_run` is captured but **not consulted** by any method here. `sync_tags_across_instances` writes tags unconditionally; `ensure_keep_set` is explicitly read-only (never writes tags). (No dry-run gating is implemented in this file.)

Singleton / concurrency: BaseManager singleton. In-memory state — `master_tag_set`, `global_tag_map`, `keep_tagged_series`, and the `_keep_set_loaded` latch — is mutated across calls; not thread-safe by design. `ensure_keep_set` is lazy + idempotent and only latches once it has actually scanned series.

Public methods:
- `refresh_tags_across_instances()` — rebuilds `global_tag_map` (`{series_id: [labels]}`) and `master_tag_set` (union of all labels) from every instance's series; also adds series with a `'keep'` **label** to `keep_tagged_series`.
- `ensure_keep_set(force=False)` — populates `keep_tagged_series` by matching on the keep tag **ID** (see below); lazy/idempotent.
- `is_series_tagged_keep(series_id)` → bool — lazily ensures the keep set, then membership-tests.
- `get_series_with_tag(tag)` → list — series ids whose `global_tag_map` entry contains `tag`.
- `sync_tags_across_instances()` — for each series on each instance, unions current tags with `master_tag_set` and writes them back (APPLY).
- `run_tag_data_pull(instance)` — for every instance, raw-GETs `/api/v3/tag`, serializes `{id,label}`, and caches it under `sonarr.<instance>.tags`. (The `instance` argument is unused; it loops over all instances.)
- `_normalize_tags(tag_list)` — internal; coerces tag dicts/values to label strings.

## How it functions

`__init__` does BaseManager wiring, `register()`, parent/dep resolution (including `instance_manager`), builds a `CacheKeyBuilder`, initializes the in-memory tag state and the `_keep_set_loaded` latch, and raises if no logger.

`ensure_keep_set(force)` — the important one for deletion protection:
1. Returns early if already loaded and not forced.
2. Looks up `SonarrCacheSeriesManager` from the registry; bails (to retry later) if it's missing or lacks `iter_all_series` — i.e. the series cache isn't warm yet.
3. Resolves instance names from `instance_manager.get_all_sonarr_apis()`; bails if none.
4. For each instance: gets keep-tag **ids** from `SonarrCacheTagManager.get_keep_tag_ids(inst)`; if empty, falls back to a live `_make_request(inst, "tag")` and filters for `label == "keep"` (case-insensitive). Then iterates `series_mgr.iter_all_series(inst)` and adds any series whose integer `tags` list intersects the keep-ids to `keep_tagged_series`.
5. Only sets `_keep_set_loaded = True` if it actually scanned at least one series (`scanned_any`), so an empty/cold cache leaves it un-latched for a later retry. Logs the loaded count.

The docstring records a real bug-fix: Sonarr series carry integer tag IDs, not labels, so the older "`'keep' in labels`" check never matched — `ensure_keep_set` matches on tag ID instead. (`refresh_tags_across_instances` still does the label-based `'keep'` check against the normalized label list it built itself.)

`run_tag_data_pull` bypasses the shared API client and issues a raw `requests.get` per instance using `base_url` + `X-Api-Key` from config, then caches a serialized `{tags, meta:{timestamp, instance, count}}` blob.

Brain delegation: none. (The `keep`-tagged set this manager maintains is consumed by deletion/lifecycle logic elsewhere — including `machine_learning` policy modules — but no decision is made here.)

## Criteria & examples

- **Keep protection is by tag ID, not label.** Example: instance `sonarr-main` has the `keep` tag at id `7`. A series whose `tags` is `[7, 12]` gets added to `keep_tagged_series`; a series with `tags = [12]` does not. `is_series_tagged_keep(<that first series id>)` returns True.
- **Live fallback.** If `SonarrCacheTagManager.get_keep_tag_ids("sonarr-main")` returns empty, the code GETs `tag` live and rebuilds keep-ids from any tag whose label lowercases to `"keep"`.
- **Lazy retry latch.** If the series cache is cold, `ensure_keep_set` scans nothing, leaves `_keep_set_loaded = False`, and a subsequent `is_series_tagged_keep` call retries.
- **Tag union sync.** If `master_tag_set = {"anime", "keep"}` and a series currently has `{"anime"}`, `sync_tags_across_instances` writes back `["anime", "keep"]` (order not guaranteed — it's `list(set.union(...))`).

## In plain English

Tags are like sticky labels on your shows — "keep forever," "anime," "kids." This manager is the librarian who walks every server, reads all the sticky labels, and builds one master list. Its most important job is the "keep forever" list: it figures out exactly which shows you've marked to protect (matching on the label's hidden ID number, because that's what the servers actually store), so that when the cleanup crew later goes looking for shows to delete, your protected *Bluey* or *The Princess Bride* never lands on the chopping block. It can also copy the full label set onto every show on every server, and it keeps a tidy cached copy of each server's label table.

## Interactions

- **Parent** — `SonarrSyncManager` (registered as `SonarrSyncTags`).
- **Sibling submanagers** — `SonarrSyncCustomFormatsManager`, `SonarrSyncFoldersManager`, `SonarrSyncMediaManager`, `SonarrSyncNamingManager`.
- **Other managers** — `SonarrCacheSeriesManager` (`iter_all_series`), `SonarrCacheTagManager` (`get_keep_tag_ids`), and `instance_manager` (`get_all_sonarr_apis`).
- **Services** — Sonarr API (`tag` GET, series GET/tag-update); `GlobalCacheManager` for the per-instance tag cache; raw `requests` for `run_tag_data_pull`.
- **Brain modules** — none invoked here; the `keep` set it produces feeds downstream deletion/lifecycle policy (including `machine_learning`), but the decision is made elsewhere.
