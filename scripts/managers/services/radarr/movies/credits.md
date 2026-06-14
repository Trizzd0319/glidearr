# RadarrMovieCreditsExtractorManager

- **File** — `scripts/managers/services/radarr/movies/credits.py`
- **One-liner** — Parses Radarr's nested `credits` field into structured cast/crew buckets (actors, directors, producers, writers, composers, editors, cinematographers, other) plus studio, and caches a per-instance people+studio map.

## What it does (for a senior Python engineer)

`RadarrMovieCreditsExtractorManager(BaseManager, ComponentManagerMixin)`. Parent is `RadarrMoviesManager`; loads no submanagers. Instance resolution via `_resolve_instance` (instance_manager → radarr_api → `"default"`).

Public methods:
- `get_credits(instance, movie_id) -> dict` — GET `movie/{movie_id}` (fallback `None`); returns `_extract_credits(movie)` or `{}` if the movie isn't found.
- `get_people_and_studios(instance) -> dict` — cache-first. Reads global_cache key `radarr.credits.{resolved}`; if warm, returns it. On a miss, GETs `movie` (fallback `[]`) and builds `{movie_id: {"title", "year", "tmdb_id", "credits": {...}, "studio"}}`, writes it to `radarr.credits.{resolved}`, and returns it.
- `get_bulk_credits(instance, movie_ids) -> list` — loops `movie_ids`, calls `get_credits` per id, and returns `[{"movie_id", "credits"}]` for the non-empty ones (one HTTP GET per id — N requests).

Internal helper: `_extract_credits(movie) -> dict` — reads `movie["credits"]["castMembers"]` and `["crewMembers"]`. Cast members become `{name, character, tmdb_id, order}` (name resolved as `name or personName`, tmdb_id as `tmdbId or personTmdbId`, default `order=999`). Crew members are bucketed by a **case-insensitive substring match** on the `job` (and sometimes `department`):
  - `"director" in job` → directors
  - `"producer"`/`"executive"` → producers
  - `"writer"`/`"screenplay"`/`"story"` → writers
  - `"composer"` in job or `"music"` in department → composers
  - `"editor"` in job or `"editing"` in department → editors
  - `"cinematography"` in department or `"director of photography"` in job → cinematographers
  - everything else → other_crew

FETCH / CACHE / APPLY: FETCH (`movie`, `movie/{id}`) + CACHE (`radarr.credits.{resolved}`). No APPLY.

API endpoints: `movie` (GET), `movie/{id}` (GET).

Config keys: none. dry_run: captured but irrelevant.

global_cache keys: read+write `radarr.credits.{resolved}`. Parquet: none.

Singleton/threading: BaseManager singleton; no threads. Note `get_bulk_credits` is N sequential GETs (no batching).

## How it functions

Init wires shared deps and logs a debug line. The library-wide path (`get_people_and_studios`) is the cached one used by the "relational cache builder"; the single/bulk paths (`get_credits`, `get_bulk_credits`) are uncached per-movie lookups. The crew classification is order-sensitive: an `if/elif` chain assigns each crew member to the **first** matching bucket.

No decision is delegated to a `machine_learning` brain module; this produces a people/studio feature artifact for downstream scoring (e.g. cast/crew affinity, related-graph).

## Criteria & examples

- Bucketing order matters: a crew member with `job="Executive Producer"` matches the producers branch (`"executive" in job`). A `job="Co-Director"` matches directors (`"director" in job`). A `job="Original Music Composer"` matches composers. A `department="Sound"`, `job="Sound Designer"` matches none of the named buckets and lands in `other_crew`.
- Cast ordering field: an actor record missing `order` is stored with `order=999`, so unranked cast sort last when a consumer sorts by `order`.
- Empty-result skip in bulk: `get_bulk_credits("default", [841, 999])` where movie 999 doesn't exist returns only `[{"movie_id": 841, "credits": {...}}]` (the empty 999 result is dropped).

## In plain English

Radarr stores a messy lump of "who worked on this movie." This manager is the credits-clerk who sorts that lump into neat piles — the actors here, the director there, the composer, the editor, and a "miscellaneous crew" pile for everyone else — and notes which studio made it. It keeps a tidy roster for the whole library so the recommendation brain can spot patterns like "you tend to love anything Christopher Nolan directs" or "you watch a lot of A24 films."

## Interactions

- **Parent manager:** `RadarrMoviesManager`.
- **Siblings:** adjacent to `enrich` (which does its own lighter people extraction) and `keywords`.
- **Services/brain:** `radarr_api` for HTTP; the people+studio map feeds the relational cache builder and `machine_learning` cast/crew affinity and related-graph scoring.
