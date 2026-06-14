# TautulliUsersManager

- **File** — `scripts/managers/services/tautulli/users/__init__.py`
- **One-liner** — Thin Tautulli adapter that fetches the household's user list and per-user playback stats, and turns pre-fetched watch history into household + per-user genre/actor/director affinity by delegating the math to the ML brain.

## What it does (for a senior Python engineer)

`TautulliUsersManager(BaseManager)` is one of the leaf submanagers loaded by `TautulliManager` (the top-level Tautulli service). It is a flat leaf: it defines **no** `load_components` call and loads **no** submanagers of its own.

It receives its `tautulli_api` HTTP client via `kwargs.get("tautulli_api")` in `__init__` (the rest — logger, config, global_cache, validator, registry — come from `BaseManager.__init__` / the shared manager tree). If `tautulli_api` is absent, the FETCH methods degrade gracefully and return `[]`.

Key PUBLIC methods:

- `get_all_users() -> list` — **FETCH.** Calls `tautulli_api.get_users()` (Tautulli `get_users` command), unwraps `response.data`, logs the count, and returns the list of user dicts. Returns `[]` if there is no API client.
- `get_user_watch_time_stats(user_id) -> list` — **FETCH.** Calls `tautulli_api.get_user_watch_time_stats(user_id=user_id)` (Tautulli `get_user_watch_time_stats`), returns `response.data`. Real-time, not cached here.
- `get_user_player_stats(user_id) -> list` — **FETCH.** Calls `tautulli_api.get_user_player_stats(user_id=user_id)` (Tautulli `get_user_player_stats`), returns `response.data`. Real-time, not cached here.
- `compute_genre_affinity(history_entries, metadata_index) -> dict` — **COMPUTE (delegated).** Produces a single household-wide affinity dict with `genres` / `actors` / `directors` count maps. The actual aggregation lives in the brain; this method adds only the summary log line and returns the result. The parent (`TautulliManager`) is what persists it.
- `compute_per_user_genre_affinity(history_entries, metadata_index, user_list) -> dict` — **COMPUTE (delegated).** Produces one affinity matrix *per Tautulli account*, keyed by username (users with zero matching history entries are omitted), e.g. `{"Trizzd": {"genres": {...}, "actors": {...}, ...}, "Aiden": {...}}`. Adds per-user debug logs and a summary log; returns the result.

Internal helpers:

- `_affinity_half_life()` — reads optional config key `scoring.affinity_half_life_days`; `None`/`0` means legacy raw counts (the default).
- `_compute_affinity_from_entries(history_entries, metadata_index)` — pure thin wrapper kept for internal / back-compat callers; same delegation as `compute_genre_affinity` minus the log.

**FETCH / CACHE / APPLY classification:** this manager does **FETCH** (user list, per-user stats) and **COMPUTE** (affinity, by delegation). It performs **no CACHE writes and no APPLY** of its own — every method either returns a value or logs. The parent `Tautullimanager.run()` is what writes the affinity results to `global_cache` (see below); it makes no PUT/DELETE/POST calls, so **dry_run is irrelevant here** (nothing mutates external state).

**External Tautulli API endpoints touched** (via `tautulli_api` → Tautulli HTTP `cmd`): `get_users`, `get_user_watch_time_stats`, `get_user_player_stats`.

**Config keys read:** `scoring.affinity_half_life_days` (optional recency decay; default off).

**global_cache / Parquet keys:** *none written by this class.* The parent `TautulliManager.run()` writes the affinity products this manager computes — `tautulli/affinity` (household) and `tautulli/users/<sanitized_username>/affinity` (per user). Downstream Radarr/Trakt scoring consume `tautulli/affinity`.

**Singleton / concurrency:** standard `BaseManager` process-wide singleton, keyed by `(class, singleton_key)`; no threading concerns of its own.

## How it functions

Lifecycle: `__init__` forwards to `BaseManager.__init__` (wiring shared logger/config/global_cache/validator/registry, self-registering under the registry "manager" category, auto-linking to its parent `TautulliManager`), then captures `tautulli_api` from kwargs. There is no `load_components`, no `prepare`, and no `run` entry point — the parent's `run()` orchestrates the sequence.

Within `TautulliManager.run()` the control flow that exercises this class is:

1. `users.get_all_users()` — the user list (real-time FETCH).
2. The parent fetches full watch history (`watch_history.get_all_history_cached`) and a metadata index (`metadata.get_metadata_index_cached`).
3. `users.compute_genre_affinity(all_entries, metadata_index)` → parent writes `global_cache["tautulli/affinity"]`.
4. `users.compute_per_user_genre_affinity(all_entries, metadata_index, user_list)` → parent loops the result and writes one `tautulli/users/<safe>/affinity` key per username (sanitizing path-forbidden characters).

The affinity **decision/computation is delegated to the machine_learning brain module `machine_learning.affinity.genre_affinity`** (functions `aggregate_affinity` and `per_user_affinity`). Per the ML-migration design intent, this service is a thin FETCH/COMPUTE adapter; the value-judgement math lives in the brain and is intentionally **not** documented here.

Note: `get_user_watch_time_stats` / `get_user_player_stats` are real-time and currently have **no caller in `run()`** — the parent removed the per-user stats fetch as dead work (results were only debug-logged, never stored). These methods remain available for a future consumer that adds caching.

## Criteria & examples

The only tunable guard in this file is the optional affinity recency decay:

- `scoring.affinity_half_life_days` unset / `0` → legacy behavior: every matching history entry contributes a raw count of 1 to its genres/actors/directors.
- `scoring.affinity_half_life_days = 30` → older watches are down-weighted by the brain. Worked example: a household watched *The Princess Bride* (genres Adventure/Romance) **60 days ago** and *Spider-Man* (genre Action) **today**. With a 30-day half-life, the 60-day-old watch is two half-lives back, contributing ~0.25 instead of 1.0, while today's watch contributes ~1.0 — so the affinity map leans toward Action far more strongly than a raw count (1 vs 1) would suggest.

Other guards are defensive, not value-judgements: missing `tautulli_api` → `[]`; a missing `response.data` path → `[]`/`{}`. Per-user matrices omit any username with zero matching history entries (so a brand-new account with no plays simply does not appear in the returned dict).

## In plain English

Think of this as the household's "taste profiler." Tautulli knows who in the house pressed play and on what. This manager first asks Tautulli "who are the users?", then takes the family's whole watch history and tallies up which genres, actors, and directors actually get watched — both for the household as a whole and for each person individually.

So if Mom keeps watching rom-coms, the kids keep replaying a cartoon, and Dad binges Marvel, the household profile shows "lots of action + animation + romance," while each person also gets their own little taste card. Later, when the app decides what to recommend or keep, it can lean on "this house loves Spider-Man-style action" rather than guessing. This manager only *gathers and tallies*; it never deletes or changes anything in your media library.

## Interactions

- **Parent manager:** `TautulliManager` (`scripts/managers/services/tautulli/__init__.py`), which constructs it as a critical component, injects the shared deps + `tautulli_api`, calls `get_all_users` / `compute_genre_affinity` / `compute_per_user_genre_affinity` in `run()`, and owns the `global_cache` writes (`tautulli/affinity`, `tautulli/users/<safe>/affinity`).
- **Sibling submanagers under `TautulliManager`:** `TautulliWatchHistoryManager`, `TautulliMetadataManager`, `TautulliSeriesManager`, `TautulliEpisodesManager`, `TautulliTranscodeManager`, `TautulliDevicesManager`, `TautulliInstanceManager`, and `TautulliValidatorManager`. In particular it depends on the *outputs* of `watch_history` (history entries) and `metadata` (metadata index), which the parent feeds into the affinity methods.
- **Brain module (delegation only, not documented here):** `machine_learning.affinity.genre_affinity` — `aggregate_affinity` (household) and `per_user_affinity` (per account).
- **HTTP client:** `tautulli_api` (`scripts/managers/services/tautulli/instances/api.py`) for the three `get_user*` endpoints.
- **Downstream consumers:** Radarr / Trakt scoring read the parent-written `tautulli/affinity` cache key.
