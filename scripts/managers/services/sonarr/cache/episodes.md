# SonarrCacheEpisodesManager

- **File** — `scripts/managers/services/sonarr/cache/episodes.py`
- **One-liner** — A thin per-series episode helper that fetches episode lists from Sonarr and answers monitoring questions (pilot/latest IDs, monitored/unmonitored splits, bulk monitoring updates).

## What it does (for a senior Python engineer)

`SonarrCacheEpisodesManager(BaseManager, ComponentManagerMixin)` is reachable as `sonarr_cache.episodes`. It is a lightweight wrapper around `self.sonarr_api.get_episodes(series_id)` plus a couple of monitoring writes.

Public methods (all keyed by `series_id`):
- `get_pilot_episode_id(series_id)` — FETCH episodes, return the `id` of the `seasonNumber==1, episodeNumber==1` episode, else None + warning.
- `get_latest_episode_id(series_id)` — FETCH episodes, return the `id` of the episode with the max `airDateUtc`.
- `build_episode_monitoring_map(series_id)` — `{episode_id: monitored_bool}` for all episodes.
- `get_monitored_episodes(series_id)` / `get_unmonitored_episodes(series_id)` — filtered lists.
- `update_episode_monitoring_state(series_id, updates)` — APPLY: builds `[{"id", "monitored"}, …]` from a `{eid: state}` dict and calls `self.sonarr_api.bulk_update_episodes(series_id, payload)`; returns True/False.
- `refresh_episode_cache(series_id)` — FETCH episodes and log the count; returns the list (note: despite the name it does not persist to a cache file — it just re-fetches).

FETCH / CACHE / APPLY: mostly **FETCH** (`get_episodes`) plus one **APPLY** (`bulk_update_episodes`). It does **not** write any global_cache / Parquet key. External API: `self.sonarr_api.get_episodes` and `self.sonarr_api.bulk_update_episodes` (the underlying Sonarr `episode` endpoints, via the API gateway).

`dry_run`: captured into `self.dry_run` in `__init__`, but the methods here do **not** check it — `update_episode_monitoring_state` will issue the bulk update regardless. (Worth noting; the heavier lifecycle writes live in `episode_files.py`, which does honour `dry_run`.)

Config keys / cache keys: none read or written.

## How it functions

Init resolves the dual-cache (`sonarr_cache` / `global_cache`), looks up `sonarr_api`, `logger`, `manager`, and `dry_run` from kwargs or the registered parent, registers itself, and raises if no logger. There is no `load_components` (no submanagers). Each public method is a single API round-trip with light filtering. No decision is delegated to a `machine_learning` module.

## Criteria & examples

- `get_pilot_episode_id`: only the exact `S01E01` matches; a series whose first available episode is `S01E02` returns None with a "No pilot episode found" warning.
- `get_latest_episode_id`: episodes are ranked by `airDateUtc` string (max). For a show whose newest episode aired `2026-05-30T01:00:00Z`, that episode's id is returned.
- `update_episode_monitoring_state(42, {101: True, 102: False})` posts `[{"id":101,"monitored":true},{"id":102,"monitored":false}]` and logs `✅ Updated monitoring state for 2 episodes in series 42`.

## In plain English

Think of a single TV box set. This helper can tell you which disc is the pilot (Season 1, Episode 1), which is the most recent, and which episodes are currently flagged to record. It can also flip those "record this" switches in bulk — for example turning recording on for the next three unwatched episodes of Bluey. It does not keep its own notebook; it just asks Sonarr each time.

## Interactions

- **Parent manager:** `SonarrCacheManager` (attached as `episodes`).
- **Services:** the `sonarr_api` gateway (`SonarrInstanceManager`) for `get_episodes` / `bulk_update_episodes`.
- **Brain modules:** none.
