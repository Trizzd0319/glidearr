# RadarrMoviesMonitoringManager

- **File** — `scripts/managers/services/radarr/movies/monitoring.py`
- **One-liner** — Reads and flips the Radarr "monitored" flag on movies — one at a time, in bulk, and queries current monitored state.

## What it does (for a senior Python engineer)

`RadarrMoviesMonitoringManager(BaseManager, ComponentManagerMixin)`. Parent is `RadarrMoviesManager`; loads no submanagers. All methods resolve the instance via `_resolve_instance` (instance_manager → radarr_api → `"default"`).

Public methods:
- `toggle_movie_monitoring(movie_id, instance, monitored) -> bool` — fetches the full movie record (via the registry: `registry.get("manager", "RadarrMoviesManager").retrieval.get_movie_by_id(resolved_instance, movie_id)`), sets `movie_data["monitored"] = monitored`, then PUT `movie/{movie_id}` with the whole record. Returns truthiness of the response. Logs a warning and returns `False` if the movie can't be found.
- `is_monitored(movie_id, instance) -> bool` — same registry-based fetch, returns `bool(movie_data.get("monitored", False))`.
- `bulk_monitor_movies(movie_ids, instance, monitor=True) -> bool` — builds payload `[{"id": mid, "monitored": monitor} for mid in movie_ids]` and PUT `movie/editor`. Returns `False` early on empty input.
- `get_monitored_movies(instance) -> list[dict]` — GET `movies` (note the trailing **s**) and filters to entries with `monitored == True`.

FETCH / CACHE / APPLY: a mix. FETCH (`is_monitored`, `get_monitored_movies`, and the read inside the toggle) + APPLY (`toggle_movie_monitoring` PUT, `bulk_monitor_movies` PUT). No caching.

API endpoints: `movie/{id}` (GET via retrieval, then PUT), `movie/editor` (PUT), `movies` (GET).

Important accuracy notes:
- `toggle_movie_monitoring` / `is_monitored` call `retrieval.get_movie_by_id(resolved_instance, movie_id)` — i.e. **arguments in the order `(instance, movie_id)`**, whereas the retrieval signature is `get_movie_by_id(movie_id, instance)`. The argument order looks transposed here; this is a code observation, not documented intent.
- `get_monitored_movies` hits the endpoint `movies` (plural), which differs from the `movie` endpoint used elsewhere in this package — likely a non-standard/legacy endpoint.

Config keys: none. dry_run: captured into `self.dry_run` but **not honored** — the PUT verbs here mutate even under dry_run. (Flag for reviewers: monitor toggles are not dry_run-aware.)

global_cache / Parquet: none. Singleton/threading: BaseManager singleton; no threads. It reaches its sibling `retrieval` indirectly through the registry rather than via a direct attribute.

## How it functions

Init wires shared deps and logs a debug line. No run-loop. The toggle path is read-modify-write: pull the current movie record, mutate the `monitored` field, PUT the whole record back (so Radarr keeps every other field). The bulk path uses Radarr's editor endpoint with a minimal `{id, monitored}` payload per movie. `get_monitored_movies` is a client-side filter over a full listing.

The *decision* of whether a movie should be monitored is made upstream by a `machine_learning` lifecycle/monitor-policy brain module (e.g. the owned-movie monitor policy); this class only applies that decision.

## Criteria & examples

- Empty-list guard: `bulk_monitor_movies([], "default")` logs "No movie IDs provided" and returns `False` with no HTTP call.
- Filter rule in `get_monitored_movies`: from a 500-movie listing where 310 have `monitored=True`, it returns those 310 and logs "Retrieved 310 monitored movies".
- Toggle example: `toggle_movie_monitoring(841, "default", monitored=False)` fetches movie 841, sets `monitored=False`, and PUTs it back; on success logs "Movie 841 is now unmonitored".

## In plain English

Monitoring is the little "watch for new/better copies of this" switch next to each movie. This manager is the person who flips that switch — for one movie, or for a whole batch at once — and who can tell you whether the switch is currently on. To flip it safely they grab the movie's full file card, tick the box, and hand the whole card back so nothing else gets lost. (For example, turning *off* monitoring for a film nobody in the house plans to upgrade, so Radarr stops hunting for a bigger 4K copy of it.)

## Interactions

- **Parent manager:** `RadarrMoviesManager`.
- **Siblings:** depends on `retrieval` (reached via the registry) to read the movie record before toggling.
- **Services/brain:** `radarr_api` for HTTP; applies monitor decisions produced by `machine_learning` monitor-policy / lifecycle modules.
