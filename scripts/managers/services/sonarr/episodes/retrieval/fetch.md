# SonarrEpisodesRetrievalFetchManager

**File** — `scripts/managers/services/sonarr/episodes/retrieval/fetch.py`
**One-liner** — Read-only lookup helpers that return cached Sonarr episodes by id / title / slug / first-letter, plus a Sonarr `history`-based "recently active" episode-id query, falling back to live API pulls when the cache misses.

## What it does (for a senior Python engineer)

A FETCH-only submanager under `SonarrEpisodesRetrieval`. Every public method reads episodes (preferring the `sonarr_cache`) and never mutates Sonarr. It resolves the target instance through `self.instance_manager.resolve_instance(instance)` on entry.

Public methods:
- `get_episode_by_id(instance, episode_id) -> dict | None` — pulls all cached episodes for the instance and linear-scans for `ep["id"] == episode_id`.
- `get_episodes_by_title(instance, title) -> list[dict]` — case-insensitive exact-title match over the cached episode list.
- `get_episodes_by_slug(instance, slug) -> list[dict]` — delegates to `self.sonarr_cache.episodes.get_by_slug(resolved, slug)`.
- `get_episodes_by_letter(instance, letter) -> list[dict]` — delegates to `self.sonarr_cache.episodes.load_letter_cache(resolved, letter)` (alphabetised cache shard).
- `get_recent_episode_ids(instance, hours=24) -> set[int]` — FETCH: hits the Sonarr `history` endpoint and returns episode ids whose record `date` is within the window.

Private helpers:
- `_get_cached_episodes_by_instance(instance) -> list[dict]` — calls `self.sonarr_cache.episodes.get_all(instance)`; on any exception it logs a warning and runs `_fetch_fallback`.
- `_fetch_fallback(instance) -> list[dict]` — last-resort live pull: `sonarr_api.get_series(instance)`, then per-series `episode?seriesId=<id>` GETs, stamping each series' cache timestamp via `sonarr_cache.episodes.set_series_timestamp(...)`.

External API endpoints touched (Sonarr, via `sonarr_api._make_request`):
- `history?page=1&pageSize=1000&sortKey=date&sortDir=desc` (in `get_recent_episode_ids`).
- `episode?seriesId=<id>` and `get_series(...)` (in `_fetch_fallback`).

FETCH / CACHE / APPLY: FETCH only. The fallback writes per-series timestamps into the Sonarr cache (a light CACHE side-effect) but performs no Sonarr APPLY.

Config keys read: none directly.
global_cache / Parquet keys: none directly — episode reads go through `self.sonarr_cache.episodes.*` (the Sonarr cache object), not `global_cache`.

dry_run: not referenced; all operations are reads, so dry_run is irrelevant here.

Concurrency: standard `BaseManager` singleton; no threading of its own. `datetime.utcnow()` is used for the recency window.

## How it functions

`__init__` resolves `self.manager` (from kwargs or `registry.get("manager", parent_name)`), then captures `global_cache`, `sonarr_cache`, `sonarr_api`, and `instance_manager` from kwargs or the parent before calling `super().__init__` and `register()`.

Main control flow per lookup: resolve instance → try the Sonarr cache → on cache failure, fall back to a live API pull and (for the fallback path) re-stamp series timestamps. `get_recent_episode_ids` is the one method that always goes live to Sonarr history, parsing each record's ISO `date` (normalising a trailing `Z` to `+00:00`) and keeping ids within `hours * 3600` seconds of `now`.

No decision is delegated to a `machine_learning` brain module.

## Criteria & examples

- **Recency window** in `get_recent_episode_ids`: a record is kept iff `(now - dt).total_seconds() <= hours * 3600`. Worked example: with the default `hours=24`, an episode grabbed/imported 23 hours ago (82,800 s ≤ 86,400 s) is included; one imported 25 hours ago (90,000 s > 86,400 s) is excluded.
- **Title match** is case-insensitive and exact: querying `"chapter one"` matches an episode titled `"Chapter One"` but not `"Chapter One: The Beginning"`.
- **Cache-miss fallback**: if `sonarr_cache.episodes.get_all("anime")` raises, the manager logs `⚠️ Fallback triggered for anime: ...` and pulls every series' episodes live, e.g. logging `🔄 Pulled 1240 episodes from API for fallback caching.`

## In plain English

This is the app's quick-lookup clerk for TV episodes. Ask it "give me episode #4821", "find every episode called 'The Rains of Castamere'", or "which episodes did we download in the last day", and it answers from a filing cabinet (the cache) so it's fast. If the cabinet drawer is jammed, it picks up the phone and calls Sonarr directly to rebuild the answer, then notes when it last refreshed that show's drawer. It only ever *reads* — it never changes what's in your library.

## Interactions

- **Parent manager:** `SonarrEpisodesRetrieval` (`SonarrEpisodesRetrievalManager`).
- **Siblings:** `enrich`, `tvdb`, `sync`, `validate`, `episode_cache`.
- **Services it talks to:** the Sonarr API (`sonarr_api`) for `history`, `episode`, and series; the Sonarr cache object (`sonarr_cache.episodes`) for `get_all` / `get_by_slug` / `load_letter_cache` / `set_series_timestamp`; `instance_manager` for instance resolution.
- **Brain modules:** none.
