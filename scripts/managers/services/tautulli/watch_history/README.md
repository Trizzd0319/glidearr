# TautulliWatchHistoryManager

- **File** — `scripts/managers/services/tautulli/watch_history/__init__.py`
- **One-liner** — A thin Tautulli adapter that fetches the full Plex watch-history feed (paginated), strips it down to non-PII fields, caches it for 24 hours, and exposes it as the master entry list every downstream Tautulli signal is derived from.

## What it does (for a senior Python engineer)

`TautulliWatchHistoryManager(BaseManager)` is one of the seven *critical* submanagers loaded by the Tautulli hub (`TautulliManager`, `scripts/managers/services/tautulli/__init__.py`) via its `load_components` map under the key `watch_history`. Its job is the FETCH and CACHE verbs for raw Plex watch history; it performs no APPLY (no PUT/DELETE/POST). It holds the Tautulli API client on `self.tautulli_api`, injected through `kwargs["tautulli_api"]` by the component loader.

Key public methods:

- `get_all_history(user_id=None, page_size=1000) -> list` — FETCH. Loops `self.tautulli_api.get_history(length=page_size, start=start, user_id=user_id)` page by page, unwrapping `resp["response"]["data"]["data"]` for each page, accumulating until `start >= recordsFiltered` or a page comes back empty. Each raw record is run through `_project_record` so only the whitelisted fields survive. Returns a flat list of projected dicts; if `self.tautulli_api` is missing it logs a warning and returns `[]`.
- `get_all_history_cached(user_id=None) -> list` — CACHE wrapper around `get_all_history`. With no `global_cache` it calls through directly. Otherwise it uses `global_cache.get_or_generate_cache` with key `tautulli/history/all` (or `tautulli/history/user/{user_id}` when a `user_id` is passed), `expiration_time=_HISTORY_TTL` (86_400 s = 24 h), and crucially `regenerate_on_expiry=True` so the cache is rebuilt when stale rather than served frozen forever.
- `get_group_movie_completions(history_entries, rating_groups_cfg) -> dict` — A pass-through to the brain. It returns `group_movie_completions(history_entries, rating_groups_cfg)` from `machine_learning.affinity.group_completion`; the manager itself adds no logic. The brain produces `{group_name: {rating_key: {"pct": float(0-1), "threshold": float}}}`. The rating_key→tmdb_id resolution and cache write are done by the parent `TautulliManager`, not here.
- `_project_record(entry) -> dict` (staticmethod) — Data-minimization. Keeps only the keys in `_CACHED_HISTORY_FIELDS`; non-dict input returns `{}`.
- `_extract_entries(data) -> list` — A defensive unwrapper that pulls an entry list out of either a bare list or a `{"response": {"data": {...}}}` shaped dict. It is a helper and is not referenced by the other methods in this file.

**FETCH / CACHE / APPLY:** FETCH (`get_all_history`) + CACHE (`get_all_history_cached`). No APPLY.

**External API touched:** the Tautulli `get_history` command via the API client (`scripts/managers/services/tautulli/instances/api.py`, which issues `_request("get_history", {length, start, user_id, section_id, media_type, search})`). Tautulli is a local service, so there is no rate-limit concern, which is why TTL-based regeneration is safe.

**Config keys read:** none directly. `rating_groups_cfg` is passed in by the caller (the parent resolves the configured rating groups, default `household`).

**global_cache keys written/read:** `tautulli/history/all` (the household-wide list) and `tautulli/history/user/{user_id}` (per-user variant). No Parquet.

**PII / data minimization:** the raw Tautulli record carries household PII. `_CACHED_HISTORY_FIELDS` projects each record down to: `user`, `user_id`, `rating_key`, `grandparent_rating_key`, `title`, `grandparent_title`, `media_type`, `percent_complete`, `platform`, `transcode_decision`, `stream_video_codec`, `stream_audio_codec`, `date`, `parent_media_index` (season number), `media_index` (episode number). The two index fields (non-PII) let the playlist watched-filter join an owned episode by `(series, season, episode)`, which survives Plex ratingKey churn — see `services/plex/playlists/tv_resolver.watched_episode_keys`. Deliberately dropped before anything hits disk: `friendly_name` (members' real display names), `ip_address` (WAN IP / location-linkable), `machine_id` (re-identifiable device fingerprint). `user_id` is retained as a non-PII stable id. NOTE: changing this projection makes the 24h-TTL `tautulli/history/*` caches schema-stale (served stale until expiry) — delete them to force regeneration.

**dry_run:** not applicable — the manager only reads and caches; it never mutates external state.

**Singleton / concurrency:** standard `BaseManager` process-wide singleton keyed by `(class, singleton_key)`; inherits logger/config/global_cache/validator/registry from the Tautulli parent. No threading of its own; pagination is a simple synchronous loop.

## How it functions

Lifecycle: `TautulliManager.prepare()` instantiates this class through `_load_component`/`load_components`, which injects the shared deps plus `tautulli_api` and sets the `…watch_history_initialized` registry flag. From then on the parent's `run()` calls `watch_history.get_all_history_cached()` once to obtain `all_entries` — the single source list every later Tautulli step (user affinity, completion stats, platform/transcode stats, group movie completions) is derived from.

Control flow inside `get_all_history`: start at `start=0`, request a page of `page_size`, unwrap and project it, advance `start += page_size`, and stop when the cumulative `start` reaches `recordsFiltered` or a page is empty. `get_all_history_cached` simply memoizes that result for 24 h with on-expiry regeneration.

Delegated decision: `get_group_movie_completions` hands the per-group, per-movie max-completion computation to the brain module `machine_learning.affinity.group_completion.group_movie_completions` (not documented here). The manager stays purely FETCH/orchestration-adjacent.

## Criteria & examples

- **Pagination stop condition** — with `page_size=1000` and Tautulli reporting `recordsFiltered=2300`: page at `start=0` (rows 0–999), `start=1000` (1000–1999), `start=2000` (2000–2299, only 300 rows). After the third page `start` becomes 3000 ≥ 2300, so the loop exits with 2300 projected entries.
- **Cache freshness** — a record fetched at 09:00 is served from `tautulli/history/all` until 09:00 the next day. Because `regenerate_on_expiry=True`, a request at 09:05 the following day triggers a fresh `get_all_history()` rather than returning the day-old list — this is the fix for history that previously "staled everything" by freezing forever.
- **Projection** — a raw record `{user: "alex", friendly_name: "Alex Real-Name", ip_address: "203.0.113.7", percent_complete: 96, …}` is cached as `{user: "alex", percent_complete: 96, …}` with `friendly_name` and `ip_address` removed; the kept `percent_complete: 96` later feeds completion/"watched" thresholds.

## In plain English

Think of this as the household's shared Netflix "Recently Watched" list, fetched from your home media server. It writes down *what* was watched and *how far* (e.g. "someone got 96% of the way through The Princess Bride"), and it keeps a once-a-day refreshed copy so the rest of the app doesn't have to keep asking. Importantly, it scrubs out the personal stuff before saving anything — it does not keep people's real names, their home IP address, or which exact device they used. It just keeps enough to know "this title was mostly finished," which is what lets the system later say "you've basically seen this, no need to recommend it again."

## Interactions

- **Parent manager:** `TautulliManager` (the Tautulli hub) — loads this class as the critical `watch_history` component and calls `get_all_history_cached()` / `get_group_movie_completions(...)` during its `run()`.
- **Sibling submanagers (same hub, no direct calls between them):** `TautulliUsersManager`, `TautulliMetadataManager`, `TautulliSeriesManager`, `TautulliEpisodesManager`, `TautulliTranscodeManager`, `TautulliDevicesManager`, plus the non-critical `TautulliInstanceManager` and `TautulliValidatorManager`. Several of them (e.g. `devices`, `transcode`) consume the `all_entries` list this manager produces.
- **Brain module:** `machine_learning.affinity.group_completion.group_movie_completions` — receives the raw history and config and returns per-group movie completion percentages (computation lives in the brain; not documented here).
- **External service:** Tautulli, via its API client's `get_history` command.
