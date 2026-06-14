# RadarrMovieEnrichmentManager

- **File** — `scripts/managers/services/radarr/movies/enrich.py`
- **One-liner** — Produces one "enriched" dict per movie (people, studio, keywords+genres, popularity, multi-source ratings, ids, file/runtime/monitored/collection) and caches the whole enriched library.

## What it does (for a senior Python engineer)

`RadarrMovieEnrichmentManager(BaseManager, ComponentManagerMixin)`. Parent is `RadarrMoviesManager`; loads no submanagers. Instance resolution via `_resolve_instance` (instance_manager → radarr_api → `"default"`).

Public methods:
- `build_enriched_movies(instance) -> list` — cache-first. Reads global_cache key `radarr.movies.{resolved}.enriched`; if warm, returns it. On a miss, GETs `movie` (fallback `[]`), maps `enrich_movie` over each, writes the list to `radarr.movies.{resolved}.enriched`, and returns it.
- `enrich_movie(movie) -> dict` — copies the raw movie and adds/overwrites: `people` (`_extract_people`), `studio`, `keywords` (= `keywords` + `genres` concatenated), `genres`, `popularity` (default 0), `ratings` (`_extract_ratings`), `tmdb_id`, `imdb_id`, `has_file` (default False), `runtime` (default 0), `monitored` (default False), `collection` (default `{}`).

Internal helpers:
- `_extract_people(movie) -> dict` — like the credits extractor but **names-only** (lists of strings, not dicts). Same `if/elif` crew bucketing into actors/directors/producers/writers/composers/editors/cinematographers (no `other_crew` bucket here).
- `_extract_ratings(movie) -> dict` — pulls `.value` from `movie["ratings"]` for `imdb`, `tmdb`, `metacritic`, `rottenTomatoes`, `trakt` (each may be `None`).

FETCH / CACHE / APPLY: FETCH (`movie`) + CACHE (`radarr.movies.{resolved}.enriched`). No APPLY.

API endpoints: `movie` (GET).

Config keys: none. dry_run: captured but irrelevant.

global_cache keys: read+write `radarr.movies.{resolved}.enriched`. This key is the one the `dataframe` sibling falls back to. Parquet: none.

Singleton/threading: BaseManager singleton; no threads.

## How it functions

Init wires shared deps and logs a debug line. The flow: `build_enriched_movies` → (miss) fetch all movies → `enrich_movie` per record → cache + return. `enrich_movie` is essentially a normalization/flattening pass that pulls the useful sub-fields up and applies safe defaults, so downstream consumers (the DataFrame builder and `machine_learning` modules) don't have to dig into Radarr's nested shape or worry about missing keys.

No decision is delegated to a `machine_learning` brain module; the enriched list is a feature artifact consumed by brain scoring/lifecycle modules.

## Criteria & examples

- Keyword concat: a movie with `keywords=["heist"]` and `genres=["Action","Crime"]` ends up with `enriched["keywords"] == ["heist", "Action", "Crime"]` (note: unlike the `keywords` submanager, enrich does **not** lowercase/dedupe — it just concatenates).
- Ratings shape: if `movie["ratings"]["imdb"]["value"] == 8.8` and there is no `metacritic` entry, `_extract_ratings` yields `{"imdb": 8.8, "tmdb": ..., "metacritic": None, "rottenTomatoes": ..., "trakt": ...}`.
- Defaults: a movie missing `runtime`, `hasFile`, `monitored`, `popularity`, and `collection` is enriched with `runtime=0`, `has_file=False`, `monitored=False`, `popularity=0`, `collection={}` — so no consumer ever hits a `KeyError`.

## In plain English

Radarr hands you a movie record that's part useful, part buried-in-folders. This manager is the assistant who fills out a clean one-page summary for each movie — who's in it, who made it, what genres and tags it has, how it's rated on IMDb/TMDb/Rotten Tomatoes, how long it runs, whether we have the file — and fills any blanks with sensible defaults so the page is never half-empty. It keeps a binder of these summaries for the whole library, which the recommendation engine reads when deciding what to suggest or trim.

## Interactions

- **Parent manager:** `RadarrMoviesManager`.
- **Siblings:** feeds `dataframe` (which reads `radarr.movies.{instance}.enriched` as its preferred source); overlaps with `credits` and `keywords` on extraction logic.
- **Services/brain:** `radarr_api` for HTTP; the enriched list is consumed by `machine_learning` scoring and lifecycle modules.
