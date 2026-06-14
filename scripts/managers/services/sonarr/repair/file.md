# SonarrRepairFileManager

**File** — `scripts/managers/services/sonarr/repair/file.py`
**One-liner** — Finds Sonarr episode-file records with incomplete metadata (missing quality or sceneName) and triggers a series refresh in Sonarr to repopulate them.

## What it does (for a senior Python engineer)

`SonarrRepairFileManager(BaseManager, ComponentManagerMixin)` is a leaf repair sub-manager under `SonarrRepairManager`. It performs **FETCH** (read episode files) and **APPLY** (trigger a Sonarr series refresh).

- **Parent:** declares `parent_name = "SonarrRepair"`. Constructed by `SonarrRepairManager` (non-critical).
- **Deps:** resolves `manager` (kwarg or `registry.get("manager", parent_name)`), then `sonarr_api`, `dry_run`, `sonarr_cache`, `global_cache`, and `instance_manager` from the manager chain. Hard-requires a `logger` (raises `ValueError` if absent).
- **Loads submanagers:** none.

Public method:

- **`repair_mismatched_file_metadata(instance_name)`** — resolves the instance, gets the API client via `sonarr_api.get_all_sonarr_apis().get(resolved_instance)` (returns early with an error log if none). It calls `api_client.episode_files()` (returns a list of dicts), and for each episode-file dict reads `id`, `seriesId`, `quality`, `sceneName`. Any file missing `quality` or `sceneName` is collected into `bad_files` and warned; unless `dry_run`, it calls `api_client.refresh_series(series_id)` to trigger a metadata refresh on the parent series. Returns the `bad_files` list.

- API endpoints touched: `episode_files`, `refresh_series`.
- Config keys read: none. global_cache keys: none read/written (caches captured but unused here).
- FETCH / CACHE / APPLY: **FETCH + APPLY**.
- dry_run: when true, the `refresh_series` call is skipped (only the warning is logged); the file is still counted in `bad_files`.
- Singleton/threading: standard `BaseManager` singleton; no threading.

## How it functions

Lifecycle: `__init__` calls `super().__init__`, `self.register()`, wires up the manager chain and both caches, and enforces the logger precondition. The single public method scans the per-instance episode-file list, and for every record judged incomplete it fires a Sonarr `refresh_series` (the standard Sonarr mechanism that re-scans and re-reads file metadata). Note that one refresh is issued per bad file even if several share a `seriesId`, so a series with multiple bad files gets refreshed multiple times. No `machine_learning` brain module is involved.

## Criteria & examples

- **Bad-metadata rule:** `not quality or not scene_name`. Example: episode file `{"id": 512, "seriesId": 88, "quality": null, "sceneName": "Show.S01E01.1080p"}` is flagged (quality missing) → warns `⚠️ Missing metadata for EpisodeFile ID 512 (Series 88)`, then (if not dry-run) calls `refresh_series(88)`. A file with both quality and sceneName present is left alone.
- **No-client guard:** if `get_all_sonarr_apis().get(resolved_instance)` is falsy, it logs `❌ No API client available for …` and returns without scanning.

## In plain English

Picture each video file as having a little spec card stapled to it — "1080p Blu-ray, source: such-and-such release." This specialist flips through the cards and spots any that are blank where the picture quality or the source name should be. When it finds one, it doesn't try to guess the answer; instead it tells Sonarr "go re-examine this whole show," so Sonarr re-reads the files and fills the cards back in. In practice mode it just notes which cards are blank without asking Sonarr to do anything.

## Interactions

- **Parent manager:** `SonarrRepairManager`.
- **Siblings:** the other `SonarrRepair*Manager` specialists.
- **Services:** the Sonarr per-instance API clients (`sonarr_api`) and `instance_manager`.
- **Brain modules:** none.
