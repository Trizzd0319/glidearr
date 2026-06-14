# TraktHistoryManager

**File** — `scripts/managers/services/trakt/history/__init__.py`
**One-liner** — Fetches the household's Trakt watch history (episodes and movies, paginated) and caches the movie watched-set that downstream scoring/pruning relies on.

## What it does (for a senior Python engineer)

`TraktHistoryManager(BaseManager, ComponentManagerMixin)` is a **FETCH + CACHE** adapter over the Trakt `sync/history` endpoint. It performs no APPLY (it never writes back to Trakt). It sets `parent_name = "TraktManager"`, calls `self.register()` after `super().__init__`, resolves `self.trakt_api` (the injected `TraktAPIManager`), and resolves `dry_run` via the canonical chain (kwargs → parent → `TraktManager` → `Main`, raising `ValueError` if unresolvable — identical to its sibling Trakt managers). Despite mixing in `ComponentManagerMixin`, it loads **no submanagers** of its own.

Public methods:
- `get_history(page=1, limit=1000) -> list | None` — one page of `GET sync/history?type=episode`. Returns `None` if no `trakt_api` is wired.
- `get_full_watch_history() -> list` — paginates episode history (100/page) until a short page; returns all items. Rate-limiting is handled centrally by `TraktAPIManager._throttle()` (no per-page sleep).
- `get_full_movie_history_cached() -> list` — the important one: returns all watched **movies**, cached under `global_cache` key **`trakt/history/movies`** for 24 h via `get_or_generate_cache(..., expiration_time=86_400, regenerate_on_expiry=True)`. This is the household movie **watched-set** that movie scoring (Group A/C) and the owned-movie prune read.
- `_fetch_full_movie_history() -> list | None` — the generator behind the cache: paginates `sync/history?type=movie`. **Returns `None` (not `[]`) on a failed/rate-limited page** so the cache layer serves the last-good copy instead of persisting a truncated/empty list.
- `fetch_all_history_threaded(max_pages=1000, limit=100) -> list` — a `ThreadPoolExecutor(max_workers=5)` parallel page fetch with a `tqdm` bar (used where order doesn't matter).
- `get_latest_episodes_by_series(episodes) -> dict` — `{tvdb_id: latest_episode}` by max `watched_at`.
- `get_history_grouped_by_series() -> dict` — `{tvdb_id: [episodes…]}`.
- `get_series_watch_counts() -> dict` — `{trakt_id: count}`.

- **Parent manager**: `TraktManager`. **Submanagers loaded**: none.
- **External API endpoint**: `GET sync/history` (with `type=episode|movie`, `page`, `limit`) via `trakt_api._make_request`.
- **global_cache keys**: writes/reads **`trakt/history/movies`** (24 h TTL, regenerate-on-expiry).
- **dry_run**: resolved via the standard chain-walk (kwargs → parent → `TraktManager` → `Main`, raises if unresolvable) for consistency with sibling managers; it has no functional effect here since this manager is read-only (no APPLY to gate).
- **Singleton / concurrency**: standard `BaseManager` singleton; `fetch_all_history_threaded` adds a 5-worker thread pool, but the cached movie path is serial.

## How it functions

Lifecycle: `__init__` → `super().__init__` (singleton + registry + parent-link to `TraktManager`) → `register()` → grab `trakt_api` + `dry_run`. There's no `run()`; callers pull history on demand. The movie path is the hot one: `get_full_movie_history_cached()` → `get_or_generate_cache("trakt/history/movies", _fetch_full_movie_history, 86_400, regenerate_on_expiry=True)`. The `None`-on-failure contract is the key design choice — a rate-limited fetch can never overwrite a good watched-set with a partial one, and because Tautulli/Plex corroborate (and push to) the same data, a stale Trakt copy loses nothing the household actually watched. No decision is delegated to a `machine_learning` brain module here — this manager only supplies the raw history other managers and the brain consume.

## Criteria & examples

- **Pagination stop**: keep fetching while a page returns the full `limit` (100); a short page ends the loop. Example: 250 watched movies → pages of 100, 100, 50 → stop after the 50-item page (3 requests).
- **Failure handling**: if page 2 of 3 comes back `None` (rate-limited), `_fetch_full_movie_history` returns `None`, so `get_or_generate_cache` serves yesterday's cached 250-movie list rather than caching a 100-movie truncation.
- **Cache freshness**: TTL 86 400 s with `regenerate_on_expiry=True` — a newly-watched movie shows up within a day, not "whenever something else triggers a refresh."

## In plain English

This is the part that asks Trakt, "what has the household actually finished watching?" — and remembers the answer for a day. That list is what lets the rest of the app know you've seen, say, *The Fellowship of the Ring* and *The Return of the King*, so it can tell that the unwatched *The Two Towers* is worth keeping in high quality. If Trakt is busy and only gives a half-answer, this deliberately keeps yesterday's full list instead of believing the half-answer — so you never get penalised for movies you really did watch just because the server hiccuped.

## Interactions

- **Parent**: `TraktManager` (constructs and injects `trakt_api`).
- **Upstream**: `TraktAPIManager` (the throttled HTTP client).
- **Downstream consumers**: the movie watched-set (`trakt/history/movies`) feeds Radarr scoring/affinity and the owned-movie stale prune; the grouped/episode helpers feed Sonarr/TV history aggregation. The `machine_learning` scorers consume this data but are called elsewhere.
