# SonarrEpisodesRetrievalTVDBManager

**File** — `scripts/managers/services/sonarr/episodes/retrieval/tvdb.py`
**One-liner** — A thin read-only client for TheTVDB v4 API that fetches series/episode/season/artwork/extended metadata, gracefully no-op'ing when no TVDB token is configured.

## What it does (for a senior Python engineer)

A FETCH-only submanager wrapping `https://api4.thetvdb.com/v4`. It is the only manager in this subtree that reaches out to an *external* (non-Sonarr) service. All requests are bearer-token GETs through `requests`; failures are swallowed and logged as warnings (returning `{}` or `[]`), so TVDB outages never propagate.

Public methods:
- `fetch_tvdb_series(tvdb_id=None, fallback_title=None)` — GET `series/<tvdb_id>` if an id is given; otherwise search by title (`search?q=<title>&type=series`) and fetch the top hit's `series/<id>`. Warns and returns `{}` if neither id nor title is usable.
- `fetch_tvdb_episode(episode_id)` — GET `episodes/<episode_id>`.
- `fetch_tvdb_episodes_by_series(tvdb_id)` — GET `series/<tvdb_id>/episodes` (returns `[]` on empty).
- `fetch_tvdb_artworks(tvdb_id)` — GET `series/<tvdb_id>/artworks`.
- `fetch_tvdb_season_episodes(tvdb_id, season_type="default")` — GET `series/<tvdb_id>/episodes?seasonType=<type>`.
- `fetch_tvdb_series_extended(tvdb_id)` — GET `series/<tvdb_id>/extended`.

Private helpers:
- `_load_token()` — reads `config["tvdb"]["token"]`; warns if absent (enrichment then "skipped").
- `_make_request(endpoint, params=None)` — returns `{}` immediately if no token; otherwise GET with `Authorization: Bearer <token>`, `raise_for_status()`, and returns the response JSON's `data` field.

External API endpoints touched: TheTVDB v4 — `series/{id}`, `episodes/{id}`, `series/{id}/episodes`, `series/{id}/artworks`, `series/{id}/extended`, `search`.

FETCH / CACHE / APPLY: FETCH only. It does **not** write to global_cache or Parquet (callers cache the results if they want); no APPLY.

Config keys read: `tvdb.token`.
global_cache / Parquet keys: none.

dry_run: not referenced — all GETs, no mutations.

Init hardening: it requires a logger (raises `ValueError` if none can be resolved from kwargs or the parent), since failure logging is its only error channel.

Concurrency: `BaseManager` singleton; uses the module-level `requests` synchronously (no session reuse, no retry/backoff beyond `raise_for_status`).

## How it functions

`__init__` → `super().__init__` + `register()` → resolve `self.manager`, ensure a logger exists, load the token, set `self.base_url`. Every public fetch funnels through `_make_request`, which short-circuits to `{}` when the token is missing — this is the "skip enrichment" path. The `data` unwrapping means callers receive the payload directly rather than the TVDB envelope.

No decision is delegated to a `machine_learning` brain module.

## Criteria & examples

- **Token gate:** with no `tvdb.token` in config, every fetch returns `{}`/`[]` and a warning is logged once at init; no HTTP is attempted.
- **Series resolution by title:** `fetch_tvdb_series(fallback_title="Stranger Things")` runs a `search?q=Stranger Things&type=series`, takes `search[0]` (the top match), and fetches that id's series record. If the search returns a non-list or empty, it falls through to the "no ID or title fallback" warning and returns `{}`.
- **Failure isolation:** a 500 from `series/12345/extended` is caught; the method logs `⚠️ TVDB request to 'series/12345/extended' failed: ...` and returns `{}` rather than raising.

## In plain English

TheTVDB is an online encyclopedia of TV shows — episode lists, air dates, poster art, the works. This manager is the app's librarian for that encyclopedia: give it a show's TVDB id (or just a title to look up) and it brings back the entry, the episode list, the artwork, or the full extended record. It needs a library card (the TVDB token) to get in — if you haven't set one up, it just shrugs and returns nothing instead of breaking. And if the encyclopedia's website is down, it quietly notes the failure and moves on. Example: ask it about "The Mandalorian" and it can pull the official season-by-season episode breakdown and posters.

## Interactions

- **Parent manager:** `SonarrEpisodesRetrieval` (`SonarrEpisodesRetrievalManager`).
- **Siblings:** `fetch`, `enrich`, `sync`, `validate`, `episode_cache`.
- **Services it talks to:** TheTVDB v4 REST API only. It does **not** call Sonarr.
- **Brain modules:** none.
