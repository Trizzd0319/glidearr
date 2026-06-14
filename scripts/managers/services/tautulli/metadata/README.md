# TautulliMetadataManager

**File** — `scripts/managers/services/tautulli/metadata/__init__.py`
**One-liner** — A thin Tautulli adapter that fetches per-item Plex metadata (genres, cast/crew, codecs, TMDB id) for a list of `rating_key`s and exposes a 7-day-cached metadata index plus a real-time library listing.

## What it does (for a senior Python engineer)

`TautulliMetadataManager(BaseManager)` is a leaf submanager of `TautulliManager`. It owns the resolution of Plex `rating_key`s into a structured metadata dict, including the `rating_key → tmdb_id` mapping that the household watched-set and genre-affinity computations depend on. It performs **FETCH** (HTTP GET to Tautulli) and **CACHE** (writes the index to `global_cache`); it never performs APPLY (no PUT/DELETE/POST).

Dependencies are injected by the parent via `kwargs`: it pulls `self.tautulli_api = kwargs.get("tautulli_api")` (the shared `TautulliAPI` instance built by `TautulliManager.__init__`), and inherits `logger`, `config`, `global_cache`, `validator`, `registry` through `BaseManager.__init__`. It loads no submanagers of its own (no `load_components` call).

Module constant: `_METADATA_TTL = 604_800` (7 days), used as the cache `expiration_time`.

### Public methods

- **`build_metadata_index(rating_keys: list) -> dict`** — The uncached worker. For each `rating_key` it makes **one** API call (`tautulli_api.get_metadata(rating_key=rk)` → Tautulli command `get_metadata`). It skips entries where the response is missing or `response.result != "success"` (logs a warning), and skips entries whose `response.data` is `{}` (Tautulli returns success-with-empty-data for items deleted from Plex; logged at debug so the key lands in the `not_in_metadata` bucket later, not the `no_tmdb_guid` bucket). For each surviving item it builds a record keyed by `rating_key` with: `genres`, `actors`, `directors`, `writers`, `composers`, `producers`, `studios` (the single `studio` field wrapped in a list), `labels`, `collections`; from the first `media_info` entry: `video_codec`, `audio_codec`, and `audio_language` (via `_extract_audio_language`); plus `view_time` (`last_viewed_at`), `tmdb_id` (via `_extract_tmdb_id`), and the debug fields `title`, `year`, `guids`, `guid`. Returns `{}` immediately if `tautulli_api` is unset. Logs a summary line with the item count.
- **`get_metadata_index_cached(rating_keys: list) -> dict`** — The cached entry point. Returns `build_metadata_index(...)` directly if `global_cache` is unset. Otherwise it performs a **schema self-heal**: it reads the existing `tautulli/metadata/index` cache and, if no value contains a `tmdb_id` field (a pre-schema-change cache), deletes the key so it rebuilds immediately rather than waiting out the 7-day TTL. It then calls `global_cache.get_or_generate_cache(key="tautulli/metadata/index", generator_function=lambda: self.build_metadata_index(rating_keys), expiration_time=_METADATA_TTL, regenerate_on_expiry=True)`. The `regenerate_on_expiry=True` flag is load-bearing: without it, expired entries would be served stale forever and newly-watched items would never resolve a `tmdbId`, silently dropping out of the household watched-set.
- **`get_library_index() -> dict`** — Real-time (uncached) library list via `tautulli_api.get_libraries()` (Tautulli command `get_libraries`). Returns `{section_id: {"name", "type", "count" (int), "active" (bool, true when `is_active == 1`)}}` for every library that has a `section_id`. Returns `{}` if `tautulli_api` is unset.

### Internal helpers

- **`_extract_audio_language(streams: list) -> list`** — De-duplicated list of `audio_language` values across streams whose `type == "2"` (Plex's audio-stream type code) and that have a non-empty language.
- **`_extract_tmdb_id(md: dict) -> int | None`** (staticmethod) — Scans `md["guids"]` (entries may be `{"id": "..."}` dicts or bare strings) for the first `tmdb://<n>` prefix and returns `int(n)`; falls back to the single `md["guid"]` field. Returns `None` if no valid TMDB GUID is found or the suffix isn't an int.

### Config keys
None read directly. (`tautulli.*` config is consumed by the parent `TautulliManager` to build the `TautulliAPI`.)

### global_cache / Parquet keys
- Reads/writes (CACHE): `tautulli/metadata/index` — the rating_key → metadata-record map; read, conditionally `delete`d for schema self-heal, and written via `get_or_generate_cache`.

### API endpoints touched (FETCH)
Both go through `TautulliAPI._request` to Tautulli's HTTP `/api/v2` endpoint:
- `cmd=get_metadata&rating_key=<rk>` — one call per `rating_key`.
- `cmd=get_libraries` — one call for `get_library_index()`.

### dry_run
No dry_run branch — this manager only FETCHes and CACHEs; it issues no mutating calls, so dry_run does not change its behavior.

### Singleton / concurrency
Instantiated through `BaseManager`'s process-wide singleton machinery (the parent loads it as the `metadata` component via `_singleton`). No threading or locks; `build_metadata_index` is a serial loop, one HTTP call per key.

## How it functions

Lifecycle: the parent `TautulliManager.__init__` constructs the shared `TautulliAPI`, then `split_components` / `_load_component` instantiate `TautulliMetadataManager` (a "critical" component) via the singleton factory, passing `tautulli_api` plus the shared deps in `init_args`. `__init__` simply forwards to `BaseManager` and stashes `self.tautulli_api`.

During `TautulliManager.run()` it is invoked twice, in order:
1. `self.metadata.get_metadata_index_cached(rating_keys)` — `rating_keys` is the de-duplicated set of `rating_key`s pulled from the cached watch history. This is the heavy step (one HTTP call per uncached key); the result feeds genre/per-user affinity and the group-completion `rating_key → tmdb_id` resolution.
2. `self.metadata.get_library_index()` — real-time library snapshot used in the run summary.

Control flow inside `get_metadata_index_cached`: cache-bypass guard → schema self-heal (delete stale pre-`tmdb_id` cache) → `get_or_generate_cache` with the 7-day TTL and `regenerate_on_expiry=True`. Inside `build_metadata_index`: per-key fetch → success/empty-data guards → record assembly via the two extractor helpers.

This manager delegates **no** decisions to a `machine_learning` brain module; it is pure FETCH/CACHE. (The downstream consumers of its `tmdb_id` and codec fields — affinity, watched-set, per-device profile selection — live in the parent run loop and elsewhere.)

## Criteria & examples

- **Empty-data skip (deleted Plex item):** Tautulli replies `{"result": "success", "data": {}}` for `rating_key=51234` because the item was removed from Plex. The empty-`data` guard logs a debug line and `continue`s, so `51234` never enters `metadata` and is later classified by the parent under `not_in_metadata` (deleted/replaced) rather than `no_tmdb_guid`.
- **TMDB extraction success:** `md["guids"] = [{"id": "imdb://tt0093779"}, {"id": "tmdb://2493"}]` → `_extract_tmdb_id` skips the imdb entry, matches `tmdb://2493`, returns `2493`. (e.g. *The Princess Bride*'s TMDB id.)
- **TMDB extraction failure (imdb-only):** `md["guids"] = [{"id": "imdb://tt0093779"}]` with `md["guid"] = ""` → returns `None`. The record still stores `title`/`year`/`guids`, so the parent run can bucket it under `no_tmdb_guid` and attempt an imdb→tmdb bridge via the Radarr movie cache.
- **Audio-language de-dup:** streams `[{"type":"2","audio_language":"English"}, {"type":"2","audio_language":"English"}, {"type":"3","audio_language":"English"}]` → `_extract_audio_language` returns `["English"]` (the second is a duplicate, the third isn't an audio stream).
- **Schema self-heal:** an existing `tautulli/metadata/index` whose records predate the `tmdb_id` field → `any("tmdb_id" in v ...)` is `False` → the key is deleted and rebuilt this run instead of being served stale until the 7-day TTL lapses.
- **Library record:** a `get_libraries` row `{"section_id": 2, "section_name": "Movies", "section_type": "movie", "count": "1843", "is_active": 1}` → `{2: {"name": "Movies", "type": "movie", "count": 1843, "active": True}}`.

## In plain English

Think of Plex as a huge DVD shelf where every disc has a barcode (the `rating_key`). This manager is the clerk who, given a stack of barcodes, walks the shelf and writes an index card for each disc: its genres, who starred in and directed it, the video/audio format, and — crucially — its universal catalog number (the TMDB id) so the same movie can be recognized no matter how it was added. If a barcode points to a disc that's been thrown out, the clerk quietly notes "not on the shelf anymore" instead of pretending it found it. Because re-checking thousands of discs is slow, the clerk keeps the index for a week before re-walking the shelf — but if the index is from an old card format that's missing the catalog number, it's thrown out and rebuilt right away. Without that catalog number, a film your household just watched — say a new Marvel release — could silently vanish from the "we've already seen this" list, and the app would keep recommending it.

## Interactions

- **Parent manager:** `TautulliManager` (`scripts/managers/services/tautulli/__init__.py`) — constructs the shared `TautulliAPI`, loads this as the critical `metadata` component, and calls `get_metadata_index_cached` then `get_library_index` inside `run()`.
- **Sibling submanagers (consumers of its output, all under `TautulliManager`):** `TautulliWatchHistoryManager` supplies the `rating_keys` (and uses the resulting `tmdb_id`s for group completions); `TautulliUsersManager.compute_genre_affinity` / `compute_per_user_genre_affinity` consume the `metadata_index`; the parent run also reads the records' `guid`/`guids` to bridge imdb→tmdb against the Radarr movie cache (`radarr.movies.standard.full`, via `RadarrManager` from the registry).
- **External services:** Tautulli HTTP API (via the injected `TautulliAPI`), commands `get_metadata` and `get_libraries`.
- **machine_learning brain:** none — this manager makes no value judgements.
