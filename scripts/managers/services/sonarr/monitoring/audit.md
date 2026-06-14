# SonarrMonitoringAuditManager

- **File** — `scripts/managers/services/sonarr/monitoring/audit.py`
- **One-liner** — Read-only data-quality auditor for a Sonarr instance's episode catalog: finds zero-byte episode files, duplicate episode titles, and episodes missing air dates.

## What it does (for a senior Python engineer)

`SonarrMonitoringAuditManager(BaseManager, ComponentManagerMixin)`, `parent_name = "SonarrEpisodesRetrieval"`. Its `__init__` differs from siblings: it resolves `self.manager` first (from `manager` kwarg or `registry.get("manager", parent_name)`), wires `global_cache` and `sonarr_cache` from it, then calls `super().__init__(... self.global_cache ...)`, registers, and resolves `sonarr_api` and `instance_manager` (the latter used to map a friendly instance name to a resolved instance). No logger guard. Loads no submanagers.

It is purely **FETCH** + **CACHE-read** for reporting — no APPLY, no cache or Sonarr writes. Every method just returns findings.

Public methods (all take an `instance: str`, first resolved via `instance_manager.resolve_instance(instance)`):
- `audit_empty_episode_files(instance) -> list[dict]` — FETCHes `sonarr_api.get_series(resolved)`, then per series does a live `sonarr_api._make_request(resolved, f"episode?seriesId={sid}")` and collects episodes where `hasFile` is true but `episodeFile.size == 0` (downloaded but zero-byte / broken file).
- `audit_duplicate_episode_titles(instance) -> dict` — reads `sonarr_cache.episodes.get_all(resolved)` (cache, not live), builds a title→ids map, and returns `{title: [ids...]}` for any title appearing more than once.
- `audit_missing_air_dates(instance) -> list[dict]` — reads `sonarr_cache.episodes.get_all(resolved)` and returns episodes whose `airDate` is falsy.

**API touched:** `sonarr_api.get_series(resolved)`, `sonarr_api._make_request(resolved, "episode?seriesId=<id>")`.
**Cache keys read:** the episodes cache via `sonarr_cache.episodes.get_all(resolved)` (a structured accessor on the Sonarr cache, not a raw string key).
**Cache/Sonarr writes:** none.
**Config keys read:** none.
**dry_run:** not captured or referenced — irrelevant, since this manager never mutates anything.

No decision is delegated to a `machine_learning` brain module; these are deterministic catalog scans.

## How it functions

Lifecycle: `__init__` (manager-first dependency resolution so the shared cache is wired before `super().__init__`) → `register()` → resolve `sonarr_api` / `instance_manager`. There is no orchestrating `run()`; a caller invokes whichever audit it wants. The empty-file audit is the heaviest — it issues one HTTP episode-list request per series — while the duplicate-title and missing-air-date audits read from the already-warmed episodes cache. Each method logs a one-line count of what it found.

## Criteria & examples

- **Empty files:** an episode flagged `hasFile = true` but with `episodeFile.size == 0` is reported. Example: S04E10 shows as "downloaded" in Sonarr but the file is 0 bytes (a failed/empty import) → it lands in the empty-files list so something else can re-grab it. An episode with `size = 734003200` is ignored.
- **Duplicate titles:** if two episodes both have title `"Pilot"` (e.g. id 51 and id 88), the result is `{"Pilot": [51, 88]}`. The first occurrence seeds the map; the second (and later) trigger a duplicate entry that includes the first id.
- **Missing air dates:** an episode with `airDate = None` or `""` is reported; one with `airDate = "2023-09-14"` is not.

## In plain English

This is the librarian who walks the shelves looking for problems — it never moves or relabels anything, it just makes a list. It spots episodes that claim to be downloaded but are actually empty files (a broken download you'd want to redo), episodes that share the exact same title (possible duplicates or mislabels), and episodes with no air date recorded (incomplete metadata). For example, it would catch a copy of a *The Office* episode that's listed as "got it!" but is really a 0-byte placeholder, so you know to grab the real file.

## Interactions

- **Parent:** `SonarrMonitoring` (declared `parent_name = "SonarrEpisodesRetrieval"`, which is how it locates its shared cache/api context).
- **Managers it talks to:** the Sonarr instance manager (`instance_manager.resolve_instance`) to translate instance names.
- **Services:** Sonarr API (live series + episode reads for the empty-file audit), Sonarr cache (the `episodes` accessor for the other two audits).
- **Brain modules:** none.
