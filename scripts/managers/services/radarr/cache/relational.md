# RadarrCacheRelationalManager

- **File** — `scripts/managers/services/radarr/cache/relational.py`
- **One-liner** — Builds and queries ML-oriented relational Parquet tables (people, movie↔person relations, studios) for a Radarr instance's movie library.

## What it does (for a senior Python engineer)

`RadarrCacheRelationalManager(BaseManager, ComponentManagerMixin)` is a relational-feature builder. It performs FETCH (it can GET the `movie` list as a fallback) and a Parquet-flavoured CACHE: it writes three Snappy-compressed Parquet files per instance. It performs no APPLY to Radarr.

Storage layout: `{global_cache.key_builder.base_dir}/radarr/<instance>/relational/`:
- `people.parquet` — deduplicated person records with aggregated stats (`PEOPLE_SCHEMA`: identity + per-role counts + avg ratings + top genres/studios + first/last year + known titles).
- `movie_person_relations.parquet` — the bipartite graph movie × person × role (`RELATIONS_SCHEMA`).
- `studios.parquet` — production-company aggregation (`STUDIOS_SCHEMA`).

Where it sits in the tree:
- **Parent**: `RadarrCacheManager` (note: `parent_name` is set to `self.__class__.__name__.replace("Manager","")` = `"RadarrCacheRelationalCache"` here, NOT `"RadarrCacheManager"` like the lighter siblings — a minor inconsistency; the actual parent at construction is still `RadarrCacheManager`).
- **Submanagers**: none.

Public methods:
- `run(instance)` — entry point. Resolves the instance; if `radarr_api is None`, warns and returns zeroed stats. Prefers the cached full movie list `radarr.movies.<instance>.full` (populated by `run_movie_data_pull`) to avoid a third 20k-movie fetch; falls back to `GET movie`. Delegates to `build_relations_from_movies`.
- `build_relations_from_movies(movies, instance)` — parses `credits.cast` / `credits.crew` from each raw movie dict, builds all three tables, saves them (unless dry_run), returns `{"movies","relation_rows","people","studios"}`.
- `build_from_movie_files_df(df, instance)` — alternative builder that reads the already-flattened `movie_files` DataFrame's pipe-separated people columns (`director_names`, `cast_names`, etc.) instead of raw credits, and writes the same three tables.
- `get_people(instance)` / `get_relations(instance)` / `get_studios(instance)` — load the respective Parquet (or an empty schema-shaped frame).
- `get_collaborators(name, instance)` — from the relations table, returns all OTHER people who shared a `movie_id` with `name`.
- `get_person_filmography(name, instance)` — all relation rows for `name`.
- `get_studio_movies(studio_name, instance)` — rows from the studios table for `studio_name`.

Internal helpers: `_resolve_instance`, `_relational_dir`, `_people_path`/`_relations_path`/`_studios_path`, `_load_parquet`, `_save_parquet`.

External API endpoints: `GET movie` (fallback only).
Config keys read: none.
Global_cache keys read: `radarr.movies.<instance>.full`. Parquet written: the three files above.

`dry_run`: respected in BOTH builders — `build_relations_from_movies` logs a `[dry_run] Would save ...` line and writes nothing; `build_from_movie_files_df` simply skips the writes when `self.dry_run`. dry_run is resolved from kwargs → parent only (no Main/registry fallback like movie_files); defaults to `False` if unresolved.

`radarr_api` resolution is hardened: kwargs/parent first, drop it if it lacks `_make_request`, then fall back to `registry.get("manager","RadarrManager").radarr_api`.

Singleton/concurrency: standard `BaseManager` singleton; pandas/pyarrow are synchronous; no threading.

## How it functions

Lifecycle: `__init__` (BaseManager wiring + hardened api/dry_run resolution + `self.register()`) → call `run(instance)` → fetch-or-reuse movie list → `build_relations_from_movies`.

`build_relations_from_movies` control flow:
1. Probes the first 5 movies for a `credits` key; if absent, logs a debug note that Radarr's bulk `/movie` endpoint omits cast/crew, so people/relations come out empty.
2. Per movie: pulls identity/ratings/genres/studios; sorts cast by `order` and keeps the top 10 as `role_type="actor"` relation rows; maps crew jobs to role types (Director→director, "...producer..." in Production→producer, Writing dept or Screenplay/Story/Writer→writer, Original Music Composer→composer, Director of Photography→cinematographer, Editor→editor) and emits relation rows; aggregates per-person and per-studio movie lists.
3. Builds the people frame (avg imdb/tmdb, top 5 genres, top 3 studios, first/last year, last-20 known titles JSON) and the studios frame (top 5 genres, top 5 directors, etc.).
4. Saves the three Parquets (gated by dry_run).

These are FEATURE tables for the ML brain (people/studio affinity, collaborative related-graph), but this file does not import or call any `machine_learning/` module — it only produces the data those modules later read.

## Criteria & examples

- Cast cap: only the 10 lowest-`order` (top-billed) cast members per movie become actor relations. Example: a film with 40 listed cast keeps just the 10 with the smallest `order`.
- Crew role mapping: a crew member with `job="Director of Photography"` becomes `role_type="cinematographer"`; a `job="Sound Mixer"` matches no rule and is dropped.
- Aggregation example: if "Patrick Stewart" appears (as actor) in 3 collected movies with imdb values 7.5, 8.0, 8.5, his people row gets `actor_count=3`, `avg_imdb_rating≈8.0`, `movie_count=3` (distinct titles), and his top genres come from the union of those films' genres.
- `get_collaborators("Cary Elwes", "default")`: finds every `movie_id` Cary Elwes is in, then returns all rows for OTHER people in those same movies (e.g. Robin Wright, Mandy Patinkin from The Princess Bride).

## In plain English

This builds the movie world's "who worked with whom" web. For every film in your library it records the cast and key crew, then tallies things like "this actor shows up in 12 of your movies and they average 7.8 on IMDb" and "this studio made 30 films you own." With that web, the system can answer questions like "show me everyone who has worked with the director of this film" — the kind of connection that powers smart recommendations. (Caveat: Radarr's bulk movie list usually omits cast/crew, so these tables fill in only once richer per-movie data is available.)

## Interactions

- **Parent**: `RadarrCacheManager`.
- **Siblings**: `RadarrCacheMovieFilesManager` — `build_from_movie_files_df` consumes that sibling's `movie_files` DataFrame; both reuse the `radarr.movies.<instance>.full` cache from the data-pull pipeline.
- **Services**: `radarr_api` (fallback fetch); `global_cache.key_builder` (Parquet base dir); pandas/pyarrow.
- **Brain modules**: none called directly; it produces feature tables that `machine_learning/` affinity/related-graph logic later reads.
