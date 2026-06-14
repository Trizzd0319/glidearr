# RadarrMoviesSyncManager

- **File** — `scripts/managers/services/radarr/movies/sync.py`
- **One-liner** — The write/APPLY leaf for movies: adds, bulk-updates, single-updates, and deletes Radarr movies (honoring dry_run on the destructive verbs).

## What it does (for a senior Python engineer)

`RadarrMoviesSyncManager(BaseManager, ComponentManagerMixin)`. Parent is `RadarrMoviesManager`; loads no submanagers. Every method resolves the instance via `_resolve_instance` (instance_manager → radarr_api → `"default"`) then calls `radarr_api._make_request`.

Public methods:
- `add_movie(movie_payload, instance)` — POST `movie` with the payload. **dry_run-guarded:** when `self.dry_run`, it logs `[dry_run] Would add movie {title} to {instance}` and returns `None` without calling the API.
- `bulk_update_movies(instance, movie_updates)` — PUT `movie/editor` with the list. Returns `False` early if `movie_updates` is empty; logs success/failure based on the (truthy) response. **Not dry_run-guarded.**
- `update_single_movie(movie_id, payload, instance)` — PUT `movie/{movie_id}` with the payload; returns the response. **Not dry_run-guarded.**
- `delete_movie(movie_id, instance, delete_files=False)` — DELETE `movie/{movie_id}?deleteFiles={true|false}`. **dry_run-guarded:** logs `[dry_run] Would delete movie {id} in {instance}` and returns `None`.

FETCH / CACHE / APPLY: pure APPLY (POST/PUT/DELETE). No FETCH, no caching.

API endpoints: `movie` (POST), `movie/editor` (PUT), `movie/{id}` (PUT), `movie/{id}?deleteFiles=...` (DELETE).

Config keys: none. dry_run: read from kwargs/parent; gates `add_movie` and `delete_movie` only — `bulk_update_movies` and `update_single_movie` will still mutate even in dry_run. Worth flagging to a reviewer: the two `editor`/single PUT verbs are not dry_run-aware.

global_cache / Parquet: none. Singleton/threading: BaseManager singleton; no threads.

## How it functions

Init wires shared deps and logs a debug line. No run-loop; callers invoke a verb when a decision (made upstream, often by a `machine_learning` lifecycle/acquisition brain module) needs to be written to Radarr. The class itself makes no decisions — it just executes the requested mutation. `delete_movie` formats the boolean `delete_files` into a lowercase query-string flag (`true`/`false`).

## Criteria & examples

- `bulk_update_movies` guard: passing an empty list logs "No movie updates provided" and returns `False` without an HTTP call; passing 12 updates issues one PUT to `movie/editor` and logs "Bulk update succeeded for 12 movies".
- `delete_movie` files flag: `delete_movie(841, "default", delete_files=True)` (non-dry-run) hits `DELETE movie/841?deleteFiles=true`, removing the movie *and* its files; with `delete_files=False` it deletes only the Radarr DB entry. Under dry_run, neither happens — only a "would delete" log line.

## In plain English

This is the clerk with the rubber stamp who actually files paperwork: adding a new movie to the catalog, changing many records at once, or pulling a movie (and optionally shredding the actual file). In "rehearsal mode" (dry_run) the clerk only narrates the destructive moves — "I *would* add this, I *would* delete that" — so you can preview the plan without anything really happening.

## Interactions

- **Parent manager:** `RadarrMoviesManager`.
- **Siblings:** complements `monitoring` (which uses `movie/editor` for monitor toggles) and `quality` (which PUTs profile changes); this class is the general add/update/delete writer.
- **Services/brain:** `radarr_api` for HTTP. Executes write decisions originating from `machine_learning` acquisition/lifecycle modules (e.g. acquisition deciding to add a movie, or a space/demote planner deciding to delete one).
