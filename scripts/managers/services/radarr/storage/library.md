# RadarrLibraryCacheManager

- **File** — `scripts/managers/services/radarr/storage/library.py`
- **One-liner** — Read-only lookup over the cached Radarr movie library (find a movie by TMDB id or title, or test presence).

## What it does (for a senior Python engineer)

`RadarrLibraryCacheManager(BaseManager, ComponentManagerMixin)` is the `library` submanager under `RadarrStorageManager`. It is a pure CACHE-reader: it never fetches from Radarr and never mutates anything.

Key PUBLIC methods:
- `get_movies_cache(instance: str) -> dict`. Resolves the instance, builds the key `f"{Paths.radarr.SONARR_LIBRARY}.{resolved_instance}"`, and returns `global_cache.load_cache(key) or {}`. Logs the entry count.
- `get_movie_by_tmdb(tmdb_id, instance) -> dict | None`. Linear scan of the cache dict's values; matches on `str(movie["tmdbId"]) == str(tmdb_id)`.
- `get_movie_by_title(title, instance) -> dict | None`. Linear scan; case-insensitive exact-match on `movie["title"]`.
- `is_movie_in_library(tmdb_id, instance) -> bool`. Thin wrapper: `get_movie_by_tmdb(...) is not None`.
- `_resolve_instance(instance)` — `instance_manager` → `radarr_api` → literal/`"default"`.
- `warm_cache(logger, cache, instance=None)` — **staticmethod**; `cache.get(f"{Paths.radarr.SONARR_LIBRARY}.{instance or 'default'}")` and logs whether it was populated.

FETCH/CACHE/APPLY: **CACHE-read only**. No FETCH (the populating writer is elsewhere, e.g. the Radarr cache/library sync managers), no APPLY. `self.dry_run` captured but irrelevant (read-only).

- External API endpoints: none.
- Config keys: none.
- global_cache keys: `f"{Paths.radarr.SONARR_LIBRARY}.{instance}"`.

> **Accuracy note (likely bug):** this file reads `Paths.radarr.SONARR_LIBRARY`, but in `scripts/support/config/cache_keys.py` the `radarr` inner class defines `RADARR_LIBRARY = "radarr/<instance>/library"` and has **no** `SONARR_LIBRARY` attribute (`SONARR_LIBRARY` exists only under the `sonarr` class). As written, every method that touches the cache key would raise `AttributeError` at runtime. Either the intended attribute is `Paths.radarr.RADARR_LIBRARY` or `Paths.sonarr.SONARR_LIBRARY` — the source is genuinely inconsistent here. (Documenting, not fixing — no `.py` edits in scope.)

- Singleton/concurrency: BaseManager singleton; self-registers; auto-links parent.

## How it functions

`__init__` is the standard storage-leaf shape: `parent_name = "RadarrStorageManager"`, `super().__init__`, `register()`, pull deps from kwargs/parent. No `load_components` (leaf). All lookups funnel through `get_movies_cache`, so the data freshness is entirely determined by whichever upstream manager wrote the library cache. No `machine_learning` delegation.

## Criteria & examples

- **TMDB match is string-normalized**: `get_movie_by_tmdb(603, "default")` matches a cached movie whose `tmdbId` is the int `603` or the string `"603"` (both coerced via `str(...)`).
- **Title match is case-insensitive exact**: `get_movie_by_title("the matrix", "default")` matches a cached `{"title": "The Matrix"}`, but **not** `"The Matrix Reloaded"` (no substring matching).
- **Empty/missing cache**: if the key is absent, `get_movies_cache` returns `{}`, so `is_movie_in_library(...)` returns `False` rather than erroring (modulo the `SONARR_LIBRARY` attribute issue above).

## In plain English

This is the catalogue clerk who only answers questions — never adds or removes anything. Ask "do we already own *The Matrix*?" or "find the film with TMDB id 603," and it flips through the pre-built index card box and tells you yes/no or hands you the card. It never walks to the shelves itself; it trusts the index someone else keeps up to date.

## Interactions

- **Parent**: `RadarrStorageManager`.
- **Siblings**: peers in the storage cluster; this one is the catalogue read-side.
- **Services**: `global_cache` (the only real dependency), `instance_manager` / `radarr_api` (resolution only). Depends on an upstream writer to populate the library cache. No brain-module delegation.
