# RadarrRepairQualityManager

- **File** — `scripts/managers/services/radarr/repair/quality.py`
- **One-liner** — Detects Radarr quality issues — movies whose quality cutoff isn't met, duplicate tmdbId entries — and can trigger upgrade searches, prioritising the most popular movies.

## What it does (for a senior Python engineer)

`RadarrRepairQualityManager` is a `BaseManager` + `ComponentManagerMixin` loaded by `RadarrRepairWrapperManager` under the key `quality`. `parent_name` derives from the class name (`RadarrRepairQuality`). Deps (`radarr_api`, `instance_manager`, `dry_run`) from kwargs-or-parent.

- **FETCH / CACHE / APPLY.**
  - FETCH: `GET movie` (the full library) — fetched fresh each call; no global_cache.
  - APPLY: `POST command` with `{"name": "MoviesSearch", "movieIds": [...]}` to trigger upgrade searches.
  - No CACHE.
- **External API endpoints:** `movie` (GET), `command` (POST `MoviesSearch`).
- **Config keys.** None read.
- **global_cache / Parquet keys.** None.
- **dry_run.** In `repair_trigger_upgrades`, true → logs `"Would trigger upgrade search …"` per movie and counts it triggered, no POST.
- **Singleton / concurrency.** `BaseManager` singleton; no threads.

Public methods:

- `find_cutoff_not_met(instance) -> list[dict]` — FETCH-only. Returns movies with `hasFile` and `movieFile.qualityCutoffNotMet` true: `{movie_id, title, year, quality_name, quality_profile_id}`. These are upgrade candidates.
- `find_duplicate_movies(instance) -> list[dict]` — FETCH-only. Groups all movies by `tmdbId`; any group with more than one entry is returned as `{tmdb_id, title, duplicates: [{movie_id, title, year, path, has_file}, ...]}`. Catches accidental re-adds / merge artifacts.
- `suggest_upgrades(instance, limit=50, candidates=None) -> list[dict]` — Ranks upgrade candidates. If `candidates` not supplied it calls `find_cutoff_not_met`. It enriches each with `popularity` (from a `{movie_id: popularity}` map built off `GET movie`) and sorts by popularity descending, then title ascending for stable order; returns the top `limit`.
- `repair_trigger_upgrades(instance, movie_ids=None, limit=20) -> stats` — APPLY. If `movie_ids` is None it derives them from `suggest_upgrades(limit=limit)`. Issues one `MoviesSearch` command per movie id. Returns `{checked, triggered, failed}`.
- `run(instance) -> dict` — The scan invoked by the wrapper. Computes `cutoff_not_met` once and reuses it: returns `{"cutoff_not_met": [...], "duplicates": [...], "upgrade_suggestions": suggest_upgrades(candidates=cutoff_not_met)}`. NOTE: read-only — it does not trigger searches.

Internal helper: `_resolve_instance`.

## How it functions

Lifecycle: `__init__` → `register()` → resolve deps → debug log. No children loaded.

`run()` is purely diagnostic and avoids a redundant fetch by threading the already-computed `cutoff_not_met` into `suggest_upgrades` as `candidates`. Mutation (`repair_trigger_upgrades`) is only invoked when called directly. All ranking logic (popularity desc) is local; no `machine_learning` delegation.

## Criteria & examples

- **Cutoff-not-met gate:** a movie must have `hasFile=True` and `movieFile.qualityCutoffNotMet=True`. A 720p file in a profile whose cutoff is 1080p → flagged for upgrade. A movie with no file is skipped.
- **Duplicate gate:** two Radarr entries both with `tmdbId=27205` (Inception) → grouped and returned; a unique tmdbId is ignored.
- **Upgrade ranking by popularity:** among candidates with popularities `12.0`, `88.5`, `3.1`, the order returned is the `88.5` movie first, then `12.0`, then `3.1`; ties broken by title. With `limit=20`, only the top 20 are returned.

## In plain English

This is the upgrade scout for your movie shelf. It walks the shelf and notes which discs are lower quality than you asked for (a 720p copy when you wanted 1080p) — those are upgrade candidates. It also spots if the same movie accidentally got added twice. When it's time to actually hunt for better copies, it goes after the crowd-pleasers first (the most popular titles), because upgrading a movie everyone watches is worth more than upgrading an obscure one. In preview mode it just lists what it would search for.

## Interactions

- **Parent manager** — `RadarrRepairWrapperManager` (loads it as `quality`).
- **Sibling submanagers** — None invoked.
- **Brain modules** — None.
- **Other services** — `radarr_api` (movie + command endpoints); `instance_manager` for resolution.
