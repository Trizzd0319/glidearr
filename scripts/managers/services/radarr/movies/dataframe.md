# RadarrMovieDataframeBuilderManager

- **File** — `scripts/managers/services/radarr/movies/dataframe.py`
- **One-liner** — Flattens enriched (or raw) Radarr movie records into a single flat pandas DataFrame for ML/analytics/auditing, cached in global_cache.

## What it does (for a senior Python engineer)

`RadarrMovieDataframeBuilderManager(BaseManager, ComponentManagerMixin)`. Parent is `RadarrMoviesManager`; loads no submanagers. Instance resolution via `_resolve_instance` (instance_manager → radarr_api → `"default"`).

Public methods:
- `build_movie_dataframe(instance) -> pd.DataFrame` — cache-first with a layered fallback source:
  1. Read global_cache key `radarr.movies.{resolved}.dataframe`; if it's a non-empty `pd.DataFrame`, return it.
  2. Else read the enriched list at `radarr.movies.{resolved}.enriched`.
  3. Else GET `movie` (fallback `[]`) and use the raw records.
  Then call `build_dataframe(...)`, write the result to `radarr.movies.{resolved}.dataframe`, and return it.
- `build_dataframe(enriched_movies) -> pd.DataFrame` — maps `_flatten_movie` over the list and wraps in `pd.DataFrame(...)`.

Internal helper: `_flatten_movie(movie) -> dict` — produces one flat row with columns: `id, title, year, runtime, tmdb_id, imdb_id, genres, keywords, studio, collection, actors, directors, producers, writers, composers, editors, cinematographers, imdb_rating, tmdb_rating, trakt_rating, metacritic, rotten_tomatoes, popularity, has_file, monitored, path, tags`. List-valued fields (genres, keywords, people roles, tags) are joined with `", "`; ratings come from the nested `ratings` dict; ids and `has_file` accept either camelCase (`tmdbId`/`hasFile`) or snake_case (`tmdb_id`/`has_file`), so the flattener works on **both** enriched and raw records.

FETCH / CACHE / APPLY: FETCH (`movie`, only as last-resort source) + CACHE (`radarr.movies.{resolved}.dataframe`). No APPLY.

API endpoints: `movie` (GET, fallback path only).

Config keys: none. dry_run: captured but irrelevant.

global_cache keys: read+write `radarr.movies.{resolved}.dataframe`; read `radarr.movies.{resolved}.enriched` (produced by the `enrich` sibling). Note the DataFrame is stored as a Python object in global_cache, not written to a `.parquet` file by this class.

Singleton/threading: BaseManager singleton; no threads.

## How it functions

Init wires shared deps and logs a debug line. The build follows a three-tier source preference (cached DataFrame → cached enriched list → live API), then flattens to one row per movie and joins multi-value fields into comma-separated strings so the table is CSV/Parquet-friendly. Because `_flatten_movie` reads both naming conventions, it tolerates either the enriched shape (from `enrich`) or the raw Radarr shape.

No decision is delegated to a `machine_learning` brain module; the DataFrame is the tabular feature substrate that brain scoring/lifecycle modules read.

## Criteria & examples

- Source preference: if `radarr.movies.default.dataframe` already holds a non-empty DataFrame, it is returned verbatim with no rebuild. If that's absent but `radarr.movies.default.enriched` holds 1,200 enriched dicts, the table is built from those. If both are absent, it falls back to one live `GET movie`.
- Field joining: a movie with `genres=["Action","Crime"]` and `people["directors"]=["Christopher Nolan"]` produces row cells `genres="Action, Crime"` and `directors="Christopher Nolan"`.
- Dual-key tolerance: a raw record using `tmdbId`/`hasFile` flattens identically to an enriched record using `tmdb_id`/`has_file` — both populate the `tmdb_id` and `has_file` columns.

## In plain English

This is the spreadsheet-maker. It takes all those one-page movie summaries and lines them up into a single big table — one row per movie, one column per fact (year, runtime, director, IMDb rating, do-we-have-the-file, and so on) — with lists collapsed into tidy comma-separated cells. That table is exactly what the number-crunching recommendation brain likes to read, so it's kept on hand and only rebuilt when needed.

## Interactions

- **Parent manager:** `RadarrMoviesManager`.
- **Siblings:** consumes the `enrich` sibling's cache (`radarr.movies.{instance}.enriched`) as its preferred input.
- **Services/brain:** `radarr_api` for HTTP (fallback only); the cached DataFrame is the tabular input to `machine_learning` scoring and lifecycle modules.
