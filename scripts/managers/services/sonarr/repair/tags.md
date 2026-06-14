# SonarrRepairTagsManager

**File** — `scripts/managers/services/sonarr/repair/tags.py`
**One-liner** — Cleans up Sonarr tags: deletes tags no series uses, and remaps old tag labels to new ones across the whole library.

## What it does (for a senior Python engineer)

`SonarrRepairTagsManager(BaseManager, ComponentManagerMixin)` is a leaf repair sub-manager under `SonarrRepairManager`. It performs both **FETCH** (read tags/series) and **APPLY** (delete tags, update series tags) against Sonarr.

- **Parent:** `self.parent_name = "SonarrRepair"`. Constructed by `SonarrRepairManager` (non-critical).
- **Deps:** resolves `sonarr_api`, `instance_manager`, `sonarr_cache` (from the `cache_manager` kwarg), and `dry_run`, each falling back to the registered parent manager's attributes (`registry.get("manager", self.parent_name)`). Raises `ValueError` if API or instance manager cannot be resolved.
- **Loads submanagers:** none.

Public methods:

- **`repair_unused_tags(instance_name)`** — resolves the instance, gets its API client, fetches `api.get_tags()` and `api.all_series()`, computes the set of tag IDs actually referenced by any series, and deletes every tag not in that set via `api.delete_tag(tag.id)`. Returns early (info log) if there are no unused tags. Returns `None`.
- **`repair_tag_map(instance_name, tag_map)`** — `tag_map` is an old→new label dict, e.g. `{"web-dl": "web", "uhd": "4k"}`. It builds a lowercase label→tag lookup from `api.get_tags()`, validates every label in the map exists (skips the whole remap if any are missing), then for each series rewrites its tag-ID list: any tag whose label is a key in the map is replaced with the mapped tag's ID, others are kept. If a series changed, it pushes the new tag list via `api.update_series(series.id, {"tags": new_tags})`. Returns `None`.

- API endpoints touched: `get_tags`, `all_series`, `delete_tag`, `update_series`.
- Config keys read: none. global_cache keys: none (`sonarr_cache` is captured but unused by these methods).
- FETCH / CACHE / APPLY: **FETCH + APPLY**.
- dry_run: when true, both methods log a `[DRY-RUN] Would …` line and skip the destructive call.
- Singleton/threading: standard `BaseManager` singleton; no threading.

## How it functions

Lifecycle: `__init__` sets `parent_name`, calls `super().__init__`, `self.register()`, resolves its deps with parent fallbacks, validates the API/instance refs, logs an init line. Each public method resolves the instance, fetches the relevant data, computes a diff/remap in memory, and either logs (dry-run) or applies via the API in a `try/except`. No `machine_learning` brain module is involved — the remap policy is supplied entirely by the caller's `tag_map`.

## Criteria & examples

- **Unused-tag rule:** a tag is deleted iff its `id` is in no series' `tags`. Example: tags are `{1:"web-dl", 2:"4k", 3:"obsolete"}` and only ids `{1,2}` are referenced — tag `3 "obsolete"` is deleted (or, under dry-run, logged as `[DRY-RUN] Would delete unused tag 'obsolete' (ID 3)`).
- **Remap validation guard:** if any old or new label in `tag_map` is absent from the instance, the entire remap is skipped with a warning. Example: `tag_map = {"web-dl": "web"}` but no tag labeled `web` exists → "Skipping remap due to missing tags: ['web']", no series touched.
- **Per-series remap:** a series tagged `[1]` ("web-dl") with map `{"web-dl":"web"}` (where "web" is tag id 5) becomes `[5]`, and is pushed via `update_series`.

## In plain English

Imagine your DVD shelf where every show wears little colored stickers ("Action", "4K", "Kids"). Over time some sticker types stop being used on anything, and some you want to rename — say all the old "Web-DL" stickers should now read "Web". This specialist throws away the sticker types nobody is using anymore, and swaps every "Web-DL" sticker for a "Web" one across the whole shelf. In a practice ("dry-run") mode it just tells you what it *would* peel off and re-stick, without touching anything.

## Interactions

- **Parent manager:** `SonarrRepairManager`.
- **Siblings:** the other `SonarrRepair*Manager` specialists.
- **Services:** the Sonarr per-instance API client (`sonarr_api`) and `instance_manager`.
- **Brain modules:** none.
