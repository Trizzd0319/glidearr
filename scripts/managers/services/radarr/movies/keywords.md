# RadarrKeywordProcessorManager

- **File** — `scripts/managers/services/radarr/movies/keywords.py`
- **One-liner** — Builds a per-instance `movie_id → normalized keywords` map from each movie's keywords + genres, cached in global_cache.

## What it does (for a senior Python engineer)

`RadarrKeywordProcessorManager(BaseManager, ComponentManagerMixin)`. Parent is `RadarrMoviesManager`; loads no submanagers. Instance resolution via `_resolve_instance` (instance_manager → radarr_api → `"default"`).

Public methods:
- `get_keywords(instance) -> dict` — cache-first. Reads global_cache key `radarr.keywords.{resolved}`; if warm, returns it. On a miss it GETs `movie` (fallback `[]`), builds the map via `build_keywords_map`, writes it to `radarr.keywords.{resolved}`, and returns it.
- `extract_keywords(movie_data) -> list` — concatenates `movie_data["keywords"]` and `movie_data["genres"]`, normalizes each (lowercase + strip) keeping only non-empty strings, dedupes via `set`, and returns a **sorted** list. (Radarr does not expose TMDb keywords directly; the docstring notes genres are used as pseudo-keywords.)
- `build_keywords_map(movies) -> dict` — `{movie["id"]: extract_keywords(movie)}` for every movie that yields a non-empty keyword list.

Internal helper: `_normalize(keyword)` → `keyword.lower().strip()`.

FETCH / CACHE / APPLY: FETCH (`movie`) + CACHE (`radarr.keywords.{resolved}`). No APPLY.

API endpoints: `movie` (GET).

Config keys: none. dry_run: captured but irrelevant (read-only + cache).

global_cache keys: read+write `radarr.keywords.{resolved}`. Parquet: none.

Singleton/threading: BaseManager singleton; no threads.

## How it functions

Init wires shared deps and logs a debug line. The data flow is: `get_keywords` → (cache miss) → fetch all movies → `build_keywords_map` iterates and calls `extract_keywords` per movie → cache + return. The normalization is intentionally simple (case-fold, trim, sort, dedupe) so the resulting sets are comparable across movies.

No decision is delegated to a `machine_learning` brain module; this class produces a feature artifact (keyword sets) that brain modules (e.g. affinity/related-graph scoring) consume.

## Criteria & examples

- Filter: only non-empty `str` entries survive normalization. A movie whose `keywords=["Heist", "  HEIST ", 42, ""]` and `genres=["Action"]` yields the normalized, deduped, sorted list `["action", "heist"]` (the `42` and the empty string are dropped, the two "heist" variants collapse).
- Map inclusion: movies that end up with an empty keyword list are **omitted** from the map (the `if kw:` guard in `build_keywords_map`). So a movie with no keywords and no genres simply won't appear as a key.

## In plain English

This is the tagger who reads each movie's labels — both its official tags and its genres — and tidies them into a clean, lowercased, alphabetized list (so "Heist" and "heist " count as the same tag). It then hands out a phone-book that maps each movie to its tidy tag list, which the recommendation brain later uses to notice things like "you seem to love heist movies." It keeps a copy of that phone-book so it doesn't have to re-read every movie each time.

## Interactions

- **Parent manager:** `RadarrMoviesManager`.
- **Siblings:** conceptually adjacent to `enrich` and `credits` (all produce ML feature artifacts from the same movie records).
- **Services/brain:** `radarr_api` for HTTP; the keyword map feeds `machine_learning` affinity / related-graph scoring modules.
