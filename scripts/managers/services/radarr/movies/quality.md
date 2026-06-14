# RadarrMoviesQualityManager

- **File** — `scripts/managers/services/radarr/movies/quality.py`
- **One-liner** — Reads and sets a movie's Radarr quality profile, and supplies a default profile when one is missing.

## What it does (for a senior Python engineer)

`RadarrMoviesQualityManager(BaseManager, ComponentManagerMixin)`. Parent is `RadarrMoviesManager`; loads no submanagers. All methods resolve the instance via `_resolve_instance` (instance_manager → radarr_api → `"default"`).

Public methods:
- `get_movie_profile_id(instance, movie_id)` — fetches the movie record (`_get_movie_data`) and returns its `qualityProfileId` (or `None`).
- `update_movie_profile(instance, movie_id, profile_id) -> bool` — read-modify-write: fetch the movie, set `movie_data["qualityProfileId"] = profile_id`, PUT `movie/{movie_id}` with the full record. Returns truthiness; warns and returns `False` if the movie can't be fetched.
- `assign_default_profile_if_missing(movie_data, instance) -> dict` — if `movie_data` has no/empty `qualityProfileId`, fills it from `get_default_quality_profile` and returns the (mutated) dict. Pure in-memory; no HTTP write.
- `get_default_quality_profile(instance)` — GET `qualityProfile`; returns the **first** profile's `id` as the default. If no profiles exist, warns and returns `1`.

Internal helper: `_get_movie_data(instance, movie_id)` — GET `movie/{movie_id}` with `fallback=None`; guards against a missing `radarr_api` and warns on an empty response.

FETCH / CACHE / APPLY: FETCH (`get_movie_profile_id`, `get_default_quality_profile`, `_get_movie_data`) + APPLY (`update_movie_profile` PUT). No caching.

API endpoints: `movie/{id}` (GET and PUT), `qualityProfile` (GET).

Config keys: none. dry_run: captured into `self.dry_run` but **not honored** — `update_movie_profile` will PUT even under dry_run. (Reviewer flag.)

global_cache / Parquet: none. Singleton/threading: BaseManager singleton; no threads.

## How it functions

Init wires shared deps and logs a debug line. No run-loop. The profile-change path mirrors the monitoring toggle: fetch the full movie record, change one field (`qualityProfileId`), PUT it back so Radarr preserves all other fields. `get_default_quality_profile` is a deliberate "first profile wins" heuristic with a hardcoded `1` fallback when Radarr reports no profiles.

The *choice* of which quality profile a movie should be on (e.g. upgrade to 4K, downgrade to 1080p) is made by `machine_learning` space/quality planner brain modules; this class only applies the resulting `profile_id`.

## Criteria & examples

- Default selection: if `GET qualityProfile` returns `[{"id": 4, "name": "HD-1080p"}, {"id": 6, "name": "Ultra-HD"}]`, `get_default_quality_profile` returns `4` (first element). If it returns `[]`, it returns `1`.
- Missing-profile fill: `assign_default_profile_if_missing({"title": "Heat", "qualityProfileId": 0}, "default")` sees the falsy `0`, fetches the default (say `4`), sets `qualityProfileId=4`, and returns the dict — without contacting Radarr to persist it (the caller is responsible for saving).
- Upgrade write: `update_movie_profile("default", 841, 6)` fetches movie 841, sets `qualityProfileId=6` (Ultra-HD), and PUTs the record back; logs "Updated quality profile for movie 841 ... to 6".

## In plain English

Every movie in Radarr has a "how good a copy do we want?" setting — DVD, 1080p, 4K, etc. This manager reads that setting and changes it when asked, and if a movie somehow has no setting at all it slaps on a sensible default (the first one on the list). It never decides on its own that *The Dark Knight* deserves a 4K upgrade — a smarter planner decides that — it just walks over and changes the dial to whatever it's told.

## Interactions

- **Parent manager:** `RadarrMoviesManager`.
- **Siblings:** complements `sync` (general writer) and `monitoring`; this is the quality-profile-specific writer/reader.
- **Services/brain:** `radarr_api` for HTTP; applies upgrade/downgrade profile decisions from `machine_learning` space and quality-planner modules.
