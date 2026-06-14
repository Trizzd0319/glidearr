# SonarrSeriesRetrievalTVDBManager

- **File** — `scripts/managers/services/sonarr/series/retrieval/tvdb.py`
- **One-liner** — TheTVDB v4 client: given a TVDB id (or a title to resolve), it fetches core + extended series metadata and seasons, normalizes them into a flat `tvdb_*` dict, and caches the result.

## What it does (for a senior Python engineer)

`SonarrSeriesRetrievalTVDBManager(BaseManager, ComponentManagerMixin)` is a small, self-contained HTTP client for `https://api4.thetvdb.com/v4`. It is the metadata-enrichment source the `enrich` manager calls per series.

**Position in the manager tree**
- Loaded by `SonarrSeriesRetrievalManager` as the `tvdb` component.
- `parent_name` derived → `"SonarrSeriesRetrievalTVDB"`.
- Deps off `kwargs["manager"]`: `sonarr_cache`, `global_cache`, `logger`, `config`.
- Builds a `requests.Session()`; if a token is configured, sets `Authorization: Bearer {token}` on the session headers.

**FETCH / CACHE / APPLY** — FETCH (HTTP GET to TVDB) + CACHE (writes the normalized result to `global_cache`). No Sonarr APPLY.

**External API endpoints** (all `GET {base_url}/{endpoint}` via the authed session, parsed as `response.json().get("data", {})`):
- `search?q={title}&type=series` — title → id fallback resolution.
- `series/{tvdb_id}` — core series record.
- `series/{tvdb_id}/extended` — extended record (genres, studios).
- `series/{tvdb_id}/episodes/official` — official seasons/episodes.

**Public method**
- `fetch_tvdb_data(tvdb_id=None, fallback_title=None, token=None, logger=None) -> dict` — returns the normalized `tvdb_*` dict (or `{}` on any failure / missing token). Cache-first: if `global_cache` has `tvdb/{tvdb_id or fallback_title}.json`, returns the cached copy. If no `tvdb_id` but a `fallback_title`, runs a `search` and takes the first result's `id`. Then pulls core + extended + seasons, assembles the result, caches it under `tvdb/{tvdb_id}.json`, and returns it.

**Internal helpers**
- `_safe_logger(msg, logger=None)` — best-effort warning logging; falls back to `print("[TVDB] …")` if the logger raises.
- `_safe_request(endpoint, params=None)` — wraps the GET; on HTTP error distinguishes 401 (token expired) and 429 (rate limit) with tailored warnings, else generic; returns the `data` payload or `{}` on any failure.

**Config keys** — `tvdb.token` (the API bearer token).
**global_cache keys** — reads/writes `tvdb/{tvdb_id or fallback_title}.json`.
**dry_run** — not handled (read/enrichment path; writes are cache-only).
**Concurrency** — owns one `requests.Session`. Note: the `enrich` manager calls `fetch_tvdb_data` from an 8-worker `ThreadPoolExecutor`, so this single session is shared across threads; `requests.Session` is generally thread-safe for concurrent GETs but there is no explicit locking here.

## How it functions

Lifecycle: `BaseManager` init → read `tvdb.token` → build session + auth header → register.

`fetch_tvdb_data` control flow:
1. Resolve token (`token or self.token`); if none → warn and return `{}`.
2. Cache hit on `tvdb/{key}.json` → return it.
3. No id but a title → `search` and adopt the first hit's `id`; no results → warn, return `{}`.
4. Still no id → warn, return `{}`.
5. `series/{id}` core; empty → warn, return `{}`. Then `series/{id}/extended` and `series/{id}/episodes/official`.
6. Assemble the flat result dict (name, slug, overview, first/last aired, year, image, status name, average runtime, country, language, aliases, genres, studios, airs day/time/timezone, seasons).
7. Cache it and return.

No `machine_learning` brain module is involved.

## Criteria & examples

- **Token gate:** with `config.tvdb.token` unset, every call short-circuits to `{}` (no HTTP).
- **Cache short-circuit:** asking for `tvdb_id=121361` when `tvdb/121361.json` already exists returns the cached dict and skips all four endpoints.
- **Title fallback:** `fetch_tvdb_data(fallback_title="Firefly")` with no id → `search?q=Firefly&type=series`, take `data[0]["id"]`, then proceed as if that id were supplied.
- **HTTP error handling:** a 401 from `series/{id}` logs `🔒 Unauthorized. TVDB token may be expired.` and that `_safe_request` returns `{}`; if it was the core call, the whole method returns `{}`.

## In plain English

Sonarr knows a show exists, but not much about it. This is the researcher who looks each show up in a big online TV encyclopedia (TheTVDB). Hand it the show's TVDB number — or just a title like "The Princess Bride" and it'll search for the number first — and it comes back with a tidy fact sheet: the official name, the year, the network it airs on, the genres, the season list, and so on. To save time and avoid annoying the encyclopedia's front desk (rate limits), it keeps a photocopy of every fact sheet it has already pulled and hands that back instead of asking again.

## Interactions

- **Parent manager:** `SonarrSeriesRetrievalManager`.
- **Primary caller (sibling):** the `enrich` manager calls `fetch_tvdb_data` once per series (from a thread pool) to attach metadata.
- **Services:** TheTVDB v4 REST API (its own `requests.Session`); `global_cache` for the `tvdb/*.json` cache.
- **Brain modules:** none.
