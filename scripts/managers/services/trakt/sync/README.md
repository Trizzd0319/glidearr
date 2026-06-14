# TraktSyncManager

- **File** — `scripts/managers/services/trakt/sync/__init__.py`
- **One-liner** — A thin Trakt read-adapter that fetches a user's TV **collection** and **watched** data (and per-show progress) and answers simple "was this show watched recently?" questions.

## What it does (for a senior Python engineer)

`TraktSyncManager(BaseManager, ComponentManagerMixin)` is a leaf service submanager that wraps a handful of Trakt **shows** sync endpoints. It performs **FETCH only** — every public method is an HTTP GET routed through the shared Trakt API client; it does no `CACHE` (it never writes to `global_cache` / Parquet) and no `APPLY` (no PUT/DELETE/POST). Consequently `dry_run` is captured (`self.dry_run`, defaulting from the parent manager) but is never consulted, because there are no mutating calls.

**Position in the manager tree.** Despite `parent_name = "TraktManager"`, this class is actually constructed and owned by the **Trakt API manager** (`scripts/managers/services/trakt/api/__init__.py`). That parent imports it lazily and instantiates it in a plain loop (NOT via `load_components`), registering it as the `sync` attribute and injecting `trakt_api=self` along with the shared deps (`logger`, `config`, `global_cache`, `validator`, `registry`, `manager`, `dry_run`). So its operational parent is the TraktAPI client; its `parent_name` points one level higher to the top-level `TraktManager`. It loads **no submanagers of its own** (it inherits `ComponentManagerMixin` but never calls `load_components`).

**Key public methods**

- `get_collection() -> list` — `GET sync/collection/shows`; returns the list of shows in the user's Trakt collection (or `[]`).
- `get_watched() -> list` — `GET sync/watched/shows`; returns the user's watched-shows history summary (or `[]`).
- `get_watched_episodes(show_trakt_id) -> dict` — `GET shows/{show_trakt_id}/progress/watched` with `params={"hidden": False, "specials": False}`; returns Trakt's watched-progress object for one show (aired/completed counts, next episode, etc.). Note the path uses a **Trakt** show id.
- `last_watched_within_threshold(tvdb_id, days=90) -> bool` — convenience predicate: `True` if the most recent watched episode for the show has a `watched_at` timestamp within `days` of now; `False` if there is no history, no timestamp, or on any parse error.
- `get_last_watched_episode(tvdb_id) -> dict | None` — returns the single most recently watched episode for a show, by sorting that show's episode history descending on `watched_at`. Note it keys on **tvdb_id** (not Trakt id).

**External API endpoints touched (FETCH):** `sync/collection/shows`, `sync/watched/shows`, `shows/{id}/progress/watched`.

**Config keys read:** none directly. (Auth/session config lives in the parent TraktAPI manager.)

**global_cache / Parquet keys:** none read or written.

**Concurrency / singleton:** `BaseManager` makes it a process-wide singleton keyed by class + singleton_key. It holds no locks; all HTTP throttling/rate-limit state lives in the shared `trakt_api`.

## How it functions

Lifecycle is minimal: `__init__` sets `parent_name`, calls `BaseManager.__init__` (dependency injection + auto-link to parent), `self.register()`s into the registry "manager" category, then reads `dry_run` and `trakt_api` out of `kwargs`. There is no `load_components` step and no dedicated `run()` entry point — callers invoke the individual fetch/query methods on demand.

Control flow funnels through two private helpers:

- `_get(endpoint, params=None)` — returns `None` if `trakt_api` was never injected; otherwise delegates to `self.trakt_api._make_request(endpoint, params=params)` (a GET). This is the single choke point for all live Trakt fetches in this class.
- `_get_episode_history(tvdb_id) -> list` — if the sibling **history** submanager exists and is populated, it calls `self.trakt_api.history.get_history_grouped_by_series()` and returns the list for the given `tvdb_id` (else `[]`). So the "last watched" helpers piggy-back on the already-fetched full watch history rather than issuing a new request.

No decision is delegated to a `machine_learning` brain module — this class only fetches and answers a date comparison.

One subtlety worth flagging: `get_history_grouped_by_series()` (in the sibling `history` manager) maps `tvdb_id → list of Trakt *episode* objects`. `get_last_watched_episode` then sorts those by `x.get("watched_at", "")`. Standard Trakt episode objects do not themselves carry a top-level `watched_at`, so unless the history layer enriches each episode with that field, the sort is effectively on empty strings and `last_watched_within_threshold` will tend to return `False`. The behavior is unclear from this file alone; it depends on what the history submanager stores per episode.

## Criteria & examples

The only threshold in the file is the recency window in `last_watched_within_threshold`:

- Default `days = 90`. The method parses the latest episode's ISO `watched_at` (normalizing a trailing `Z` to `+00:00`), computes `delta = now - watched_at`, and returns `delta.days <= days`.

Worked examples (assume today is 2026-06-10):
- A show whose newest episode was watched on **2026-04-01** → `delta.days ≈ 70`, `70 <= 90` → returns **True** ("watched recently").
- A show last watched on **2025-12-01** → `delta.days ≈ 191`, `191 <= 90` is false → returns **False**.
- A show with no history, a missing `watched_at`, or a malformed timestamp → returns **False** (defensive `except` paths).

## In plain English

Think of this as the part of Glidearr that phones up Trakt and asks two simple things: "Which TV shows does this person own/track?" and "When did they last actually watch an episode of show X?" It is read-only — it never changes anything on Trakt, it just reports back. For example, if you binged *The Mandalorian* two months ago, this helper would say "yes, watched within the last 90 days," which lets the rest of the app treat that show as still-active rather than stale. It is the friendly note-taker, not the decision-maker.

## Interactions

- **Operational parent:** the TraktAPI client (`trakt/api/__init__.py`), which constructs it as the `sync` submanager and supplies `trakt_api=self`; all HTTP goes through that client's `_make_request`.
- **Declared parent (`parent_name`):** `TraktManager` (the top-level Trakt service manager).
- **Sibling submanager it reads from:** `TraktHistoryManager` (`history`), via `get_history_grouped_by_series()` for the "last watched episode" lookups.
- **Submanagers it loads:** none.
- **Brain modules:** none — no `machine_learning` delegation.
