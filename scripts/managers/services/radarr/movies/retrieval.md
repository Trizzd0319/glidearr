# RadarrMoviesRetrievalManager

- **File** — `scripts/managers/services/radarr/movies/retrieval.py`
- **One-liner** — Read-only Radarr movie fetcher: pulls movies (all, by id, by TMDb id, in "chunks"), the movie history, metadata definitions, and a per-instance cached library.

## What it does (for a senior Python engineer)

`RadarrMoviesRetrievalManager(BaseManager, ComponentManagerMixin)` is the FETCH (and partial CACHE) leaf for movies. Parent is `RadarrMoviesManager`. It loads no submanagers of its own. Every method begins by calling `_resolve_instance(instance)` — preferring `instance_manager.resolve_instance`, then `radarr_api.resolve_instance`, finally `instance or "default"` — and then issues `self.radarr_api._make_request(resolved, endpoint, ...)`.

Public methods:
- `get_all_movies(instance)` — GET `movie`, `fallback=[]`. The full library.
- `get_movie_by_id(movie_id, instance)` — GET `movie/{movie_id}`, `fallback={}`.
- `get_movie_by_tmdb(tmdb_id, instance)` — GET `movie?tmdbId={tmdb_id}`, `fallback=[]`; returns the first element (or the dict). On miss, logs a warning and **falls back to a full-library scan** matching on `movie["tmdbId"] == tmdb_id`; returns `None` if still not found.
- `get_all_movies_chunked(instance, chunk_size=200)` — Radarr v3 `/movie` has no server-side pagination, so this just GETs the full `movie` list and returns it whole (the `chunk_size` arg is currently a no-op; the docstring describes the intent but the body returns the full list).
- `get_movie_history(instance)` — GET `history`, `fallback=[]`.
- `get_library(instance)` — cache-first: reads global_cache key `radarr.movies.{resolved}.library`; if warm, filters to dicts that have an `"id"` and returns them. On a miss it calls `_fetch_full_library` (GET `movie`), filters the same way, and writes the filtered list back to that cache key.
- `get_metadata(instance)` — GET `metadata`, returns `[]` if falsy.
- `get_movie_by_id_from_cache(instance, movie_id)` — reads a gzipped JSON file from disk at `cache/radarr/library/{resolved}/movie_{movie_id}.json.gz`; returns `None` if missing or unreadable. (Note: this on-disk file is read here but is written elsewhere, not by this class.)

Internal helpers: `_resolve_instance`, `_fetch_full_library(instance)` (GET `movie`), `_fetch_movie_by_id(instance, movie_id)` (GET `movie/{movie_id}`).

FETCH / CACHE / APPLY: FETCH (all the GETs) plus CACHE in exactly one place — `get_library` populates `radarr.movies.{resolved}.library`. No APPLY (no PUT/POST/DELETE).

API endpoints touched: `movie`, `movie/{id}`, `movie?tmdbId={id}`, `history`, `metadata`.

global_cache keys: read+write `radarr.movies.{resolved}.library`. On-disk read: `cache/radarr/library/{resolved}/movie_{id}.json.gz`.

Config keys: none. dry_run: captured but irrelevant (all reads).

## How it functions

Init wires the shared deps (radarr_api, instance_manager, manager=parent, dry_run) and logs a debug line. There is no run-loop; this is a request-driven helper. The only stateful behavior is the `get_library` cache: first call fetches+filters+stores, later calls within the process return the cached list. `get_movie_by_tmdb` has a two-stage strategy (direct query, then full-scan fallback) to tolerate Radarr's occasionally-flaky `?tmdbId=` filter.

No decision is delegated to a `machine_learning` brain module; this class only supplies raw movie data that brain modules later score.

## Criteria & examples

- `get_library` validity filter: a cached entry is only returned if `isinstance(m, dict) and "id" in m`. Example: a warm cache of 1,200 entries where 3 are malformed (missing `id`) returns 1,197 movies.
- `get_movie_by_tmdb` fallback: querying `tmdbId=27205` (Inception) returns the matching movie if `movie?tmdbId=27205` yields anything; if that returns empty, it scans the full library for a movie whose `tmdbId == 27205`, and only if none matches does it log "not found" and return `None`.

## In plain English

This is the librarian who only ever *looks things up* — never changes anything. Ask "do we have Inception?" and they first check the quick index by its catalog number; if the index hiccups, they walk the shelves one by one to be sure. They also keep a photocopied list of the whole collection in a drawer so the next person who asks doesn't have to wait for a fresh walk-through.

## Interactions

- **Parent manager:** `RadarrMoviesManager` (which exposes `get_all_movies` / `get_movie_by_id` as pass-throughs to this class).
- **Siblings:** referenced by `monitoring` and `helpers` (they reach `RadarrMoviesManager.retrieval.get_movie_by_id` via the registry).
- **Services/brain:** `radarr_api` for HTTP; the cached library and history feed downstream `machine_learning` scoring/lifecycle modules indirectly.
