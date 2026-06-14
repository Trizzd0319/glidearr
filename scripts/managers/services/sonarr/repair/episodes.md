# SonarrRepairEpisodesManager

**File** — `scripts/managers/services/sonarr/repair/episodes.py`
**One-liner** — Scans Sonarr at the episode level for files with broken/missing path links and for episode metadata whose backing file no longer exists, logging each issue (the actual fix logic is a TODO stub).

## What it does (for a senior Python engineer)

`SonarrRepairEpisodesManager(BaseManager, ComponentManagerMixin)` is a leaf repair sub-manager under `SonarrRepairManager`. Today it is effectively **FETCH + report only**: both methods detect and log problems, with the write-side repair left as an explicit `# TODO` (the `if not self.dry_run: pass` branches do nothing).

- **Parent:** declares `parent_name = "SonarrRepair"`. Constructed by `SonarrRepairManager` (non-critical).
- **Deps:** resolves `manager` (kwarg or `registry.get("manager", parent_name)`), then `sonarr_api`, `instance_manager`, `key_builder`, `dry_run`, a `sonarr_cache` (the per-service cache) **and** `global_cache` — the "dual cache setup" noted in source. None are hard-required by a raise.
- **Loads submanagers:** none.

Public methods:

- **`repair_missing_files(instance_name)`** — resolves the instance, reads `…[instance].episode_files.all()`, selects episode files where `not ep.path or not ep.relativePath`, and logs a warning per missing link (`S{season:02}E{episode:02} - ID …`). The repair branch is a TODO no-op. Returns `None`.
- **`cleanup_orphaned_episodes(instance_name)`** — resolves the instance, reads `…[instance].episode.all()` (episode metadata) and `…[instance].episode_files.all()` (files), then flags episodes whose `episodeFileId` is non-zero but not present in the set of live file IDs. Logs a warning per orphan; cleanup branch is a TODO no-op. Returns `None`.

- API endpoints touched (via the per-instance client): `episode_files.all`, `episode.all`.
- Config keys read: none. global_cache keys: none read/written (caches are captured but unused by current logic).
- FETCH / CACHE / APPLY: **FETCH only** today; APPLY is stubbed.
- dry_run: checked, but since the non-dry-run branch is a `pass`, behavior is identical either way (report-only).
- Singleton/threading: standard `BaseManager` singleton; no threading.

## How it functions

Lifecycle: `__init__` calls `super().__init__`, `self.register()`, then wires up the manager chain and both caches. Each public method resolves the instance through `instance_manager.resolve_instance` and reads from the per-instance API client. Detection is pure set/attribute logic; the corrective action is not yet implemented (clearly marked TODO). No `machine_learning` brain module is involved.

## Criteria & examples

- **Missing-file rule:** `not ep.path or not ep.relativePath`. Example: episode file ID 318 for S02E05 has `relativePath = None` → logged as `⚠️ Missing episode link: S02E05 - ID 318`; no change made.
- **Orphan-metadata rule:** `getattr(ep, "episodeFileId", 0) and ep.episodeFileId not in file_ids`. Example: episode S01E10 has `episodeFileId=900` but no episode file with id 900 exists → logged as `🗑️ Orphaned metadata: S01E10 (episodeFileId=900)`; no change made.

## In plain English

This is the specialist who checks each individual episode rather than whole shows. He looks for two problems: an episode the catalog thinks has a video file but the "where is it?" address is blank, and an episode card that points to a video file that's gone missing. Right now he only writes down what's wrong on a sticky note — the part where he'd actually re-shelve or fix it is marked "to be built later." So think of him as a diagnostician, not yet a repairman.

## Interactions

- **Parent manager:** `SonarrRepairManager`.
- **Siblings:** the other `SonarrRepair*Manager` specialists.
- **Services:** the Sonarr per-instance API clients (`sonarr_api`) and `instance_manager`; holds references to both `sonarr_cache` and `global_cache` for future use.
- **Brain modules:** none.
