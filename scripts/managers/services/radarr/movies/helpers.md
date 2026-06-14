# RadarrMoviesHelperManager

- **File** — `scripts/managers/services/radarr/movies/helpers.py`
- **One-liner** — Grab-bag of small movie utilities: lookup by TMDb id, title-slug retrieval, title sanitization, tmdb-id extraction, tag listing, and a couple of deprecated TVDb shims.

## What it does (for a senior Python engineer)

`RadarrMoviesHelperManager(BaseManager, ComponentManagerMixin)`. Parent is `RadarrMoviesManager`; loads no submanagers. Unlike the other leaves, several methods call `self.instance_manager.resolve_instance(instance)` directly (no `_resolve_instance` wrapper is defined on this class), so they assume `instance_manager` is present.

Public methods:
- `get_movie_by_tmdb(instance, tmdb_id)` — guards against a falsy `tmdb_id` (warns, returns `None`); GET `movie?tmdbId={tmdb_id}` (fallback `[]`); returns the first result (or `None` if empty).
- `get_movie_by_tvdb(instance, tvdb_id)` — **deprecated** shim. Radarr indexes by TMDb, not TVDb; logs a debug deprecation note and forwards to `get_movie_by_tmdb`.
- `get_movie_title_slug(instance, movie_id)` — resolves the movie record by preferring the `retrieval` sibling reached through the registry (`registry.get("manager", "RadarrMoviesManager").retrieval.get_movie_by_id(movie_id, resolved_instance)`); falls back to a direct GET `movie/{movie_id}`. Returns `movie_data["titleSlug"]` (or `None`).
- `sanitize_movie_title(title)` — replaces the curly apostrophe `’` with `'`, strips, lowercases.
- `extract_tmdb_id_from_movie(movie_obj)` — returns `tmdbId` or `tmdb_id` or `externalIds.tmdb`.
- `extract_tvdb_id_from_movie(movie_obj)` — **deprecated** shim; logs a note and forwards to `extract_tmdb_id_from_movie`.
- `get_movie_tags(instance)` — GET `tags` (fallback `[]`).
- `generate_movie_lookup_map(instance)` — GET `movies` (plural endpoint; fallback `[]`).

FETCH / CACHE / APPLY: FETCH only (`movie?tmdbId=`, `movie/{id}`, `tags`, `movies`). No caching, no APPLY.

API endpoints: `movie?tmdbId={id}` (GET), `movie/{id}` (GET), `tags` (GET), `movies` (GET).

Config keys: none. dry_run: captured but irrelevant. global_cache / Parquet: none.

Singleton/threading: BaseManager singleton; no threads. Reaches `retrieval` indirectly through the registry.

## How it functions

Init wires shared deps and logs a debug line. There's no run-loop — these are convenience utilities called ad hoc. The slug method illustrates the package pattern: prefer the registry-resolved sibling submanager, fall back to a raw API call. The two `tvdb` methods exist only as deprecation guards/back-compat aliases for callers that still pass TVDb ids.

No decision is delegated to a `machine_learning` brain module.

## Criteria & examples

- TMDb guard: `get_movie_by_tmdb("default", None)` short-circuits with a warning and returns `None` before any HTTP call.
- Slug fallback: `get_movie_title_slug("default", 841)` first tries `RadarrMoviesManager.retrieval.get_movie_by_id(841, "default")`; if the registry has no such manager or no `retrieval`, it directly GETs `movie/841` and returns its `titleSlug` (e.g. `"the-dark-knight-2008"`).
- Title sanitize: `sanitize_movie_title("Schindler’s List ")` → `"schindler's list"`.

## In plain English

This is the office junior with a drawer of handy tools: "find me the movie with this catalog number," "what's this film's web-friendly name," "clean up this messy title," "list all our tags." A couple of the tools are labeled "old — use the new one instead" (the TVDb ones), kept around only so older requests don't break.

---

# MoviesListHelper

- **File** — `scripts/managers/services/radarr/movies/helpers.py`
- **One-liner** — A small non-manager helper that binds a memoized "get movie list" closure per instance and reads an on-disk gzipped library index for search.

## What it does (for a senior Python engineer)

`MoviesListHelper` is a plain class (it does **not** end in `Manager` and is not a `BaseManager` — it takes `api` and `logger` directly in `__init__` and keeps a private `self._movie_cache` dict). It is included here only because it lives in the same file; it is not loaded by `RadarrMoviesManager.load_components`.

Methods:
- `bind_movie_list(instance)` — returns a closure `get_movie_list(force_refresh=False)` that GETs `movies` (plural) once per instance and memoizes the result in `self._movie_cache[instance]`; it validates the response is a list (warns and substitutes `[]` otherwise).
- `list_index(instance)` — reads `cache/radarr/library/{resolved}/index.json.gz` (gzipped JSON); returns `[]` if missing/unreadable. (Note: it calls `self.instance_manager.resolve_instance`, but `MoviesListHelper.__init__` never sets `self.instance_manager` — so this method would raise `AttributeError` unless an instance_manager is attached externally. Flagging as a code observation.)
- `search_index(instance, keyword)` — lowercases the keyword and filters `list_index` entries whose `title` or `path` contains it.

FETCH / CACHE / APPLY: FETCH (`movies`) + an in-memory memo + on-disk index reads. No APPLY.

## How it functions

Constructed with an `api` and `logger`. `bind_movie_list` is the main entry, handing back a refreshable, cached getter. `search_index` is a pure on-disk filter over a pre-built gzipped index file (written elsewhere, not by this class).

## Criteria & examples

- Memoization: the first `get_movie_list()` hits `GET movies`; subsequent calls return the cached list until `get_movie_list(force_refresh=True)` forces a re-fetch.
- Search: `search_index("default", "batman")` returns every index entry whose lowercased title or path contains `"batman"` (e.g. `Batman Begins`, `The Batman`).

## In plain English

A lightweight assistant that remembers the movie list after looking it up once (so it doesn't keep asking), and can scan a pre-made card-catalog file on disk for any movie whose name or folder mentions your search word.

## Interactions

- **Parent manager (for `RadarrMoviesHelperManager`):** `RadarrMoviesManager`.
- **Siblings:** `RadarrMoviesHelperManager` reaches `retrieval` via the registry for slug lookups.
- **Services/brain:** `radarr_api` for HTTP; `MoviesListHelper` additionally reads the on-disk gzipped library index/cache. Neither talks to a `machine_learning` brain module.
