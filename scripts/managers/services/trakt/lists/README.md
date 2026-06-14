# TraktListsManager

**File** — `scripts/managers/services/trakt/lists/__init__.py`
**One-liner** — A thin Trakt read-adapter that fetches the household's custom user lists and cross-references them against watch history to produce one unified per-show summary keyed by TVDB ID.

## What it does (for a senior Python engineer)

`TraktListsManager` is a leaf service manager under `TraktManager`. It performs **FETCH** only — it issues authenticated Trakt GET requests through the sibling `TraktAPIManager` and assembles an in-memory summary. It does no **CACHE** (it never writes to `global_cache` or Parquet) and no **APPLY** (it issues no PUT/DELETE/POST and so has no `dry_run`-gated mutation).

Position in the manager tree:
- **Parent**: `TraktManager` (declared via `parent_name = "TraktManager"`, set both as a class attribute and again in `__init__`). Through `BaseManager` auto-linking it inherits the parent's logger / config / global_cache / validator.
- **Submanagers loaded**: none. Although it mixes in `ComponentManagerMixin`, it never calls `load_components`, so it has no child managers of its own.
- **Sibling it depends on**: `self.trakt_api` (a `TraktAPIManager`, injected via the `trakt_api` kwarg). All HTTP goes through `trakt_api._make_request(...)`. It also reaches `self.trakt_api.history` (a `TraktHistoryManager` instance) to obtain grouped watch history.

Injected dependencies (read from `kwargs` in `__init__`):
- `manager` — the parent manager; used only to default `dry_run`.
- `dry_run` — defaults to the parent's `dry_run`, else `False`. Stored but never actually consulted (no mutating paths exist).
- `trakt_api` — the `TraktAPIManager` used for every request; if absent, `_get` returns `None` and the manager degrades to empty results.
- `sonarr_api` — optional, accepted and stored as `self.sonarr_api` but **unused** in this file (reserved for external Sonarr integration).

Config keys read:
- `trakt.username` — the Trakt user whose lists/history are fetched; defaults to the literal `"me"` (Trakt's self-alias) when unset. Stored as `self.user`.

Key public methods:
- `get_user_lists() -> list` — GET `users/{user}/lists`; returns the raw list of the user's custom lists (or `[]`).
- `get_list_items(list_slug) -> list` — GET `users/{user}/lists/{list_slug}/items`; returns the items in one list (or `[]`).
- `get_collected_shows() -> list` — GET `users/{user}/collection/shows`; returns the user's collected shows (or `[]`). Note: defined but not used by the summary path.
- `get_user_watched(media_type="shows") -> list` — GET `sync/watched/{media_type}`; returns the watched summary for that media type (or `[]`). Also not used by the summary path.
- `process_user_lists_summary() -> dict` — the primary entry point. Builds an index of lists → TVDB IDs, pulls grouped watch history, and merges them into a unified per-show dict (see below).

External API endpoints touched (all GET, all relative to the Trakt base URL inside `TraktAPIManager`):
- `users/{user}/lists`
- `users/{user}/lists/{slug}/items`
- `users/{user}/collection/shows`
- `sync/watched/{media_type}`

Concurrency / singleton notes: as a `BaseManager` it is a process-wide singleton cached by `(class, singleton_key)`. It spawns no threads itself; any threading lives downstream in `TraktHistoryManager.fetch_all_history_threaded` / `TraktAPIManager`. Rate-limit handling (429 caps, token refresh) is entirely the API manager's concern.

## How it functions

Lifecycle:
1. `__init__` calls `super().__init__(...)` (BaseManager wires shared deps and parent auto-link), then `self.register()`, then pulls `manager`, `dry_run`, `trakt_api`, and `sonarr_api` out of `kwargs` and resolves `self.user` from `config["trakt"]["username"]`.
2. There is no `load_components` step and no long-running `run`. Callers invoke `process_user_lists_summary()` directly.

`process_user_lists_summary()` control flow:
1. `get_user_lists()` → raw lists.
2. `_build_list_index(lists)` → `{list_slug: [tvdb_id, ...]}`. For each list it reads `ids.slug`, fetches that list's items via `get_list_items(slug)`, and extracts `item["show"]["ids"]["tvdb"]` for every entry that has a `show` block with `ids`. Falsy/missing TVDB IDs are dropped.
3. `_get_history_grouped()` → grouped watch history. It guards on `self.trakt_api.history` being present and, if so, delegates to `trakt_api.history.get_history_grouped_by_series()`, which returns `{tvdb_id: [episode_dict, ...]}`. If the history submanager is missing it returns `{}`.
4. `_generate_unified_summary(history_grouped, index)` merges the two into the final dict (below) and logs one summary line.

`_generate_unified_summary(history_by_series, list_index)`:
- Seeds a `defaultdict` whose default record is `{"title": "", "in_library": False, "lists": set(), "episodes_watched": 0, "last_watched": None}`.
- First pass: for every `(list_slug, [tvdb_ids])`, add `list_slug` to each show's `lists` set. (A show on no list never appears unless it also has history.)
- Second pass: for every `(tvdb_id, history)` it sets `episodes_watched = len(history)` and computes `last_watched` as the `max` of the episodes' `watched_at` strings, parsed via `datetime.fromisoformat` after rewriting a trailing `Z` to `+00:00`. A `ValueError` on parse leaves `last_watched = None`.
- Returns a plain `dict` (the defaultdict is materialized).

Internal helper `_get(endpoint, params=None)` is the single funnel for all reads: it returns `None` if `trakt_api` is missing, otherwise `trakt_api._make_request(endpoint, params=params)`.

**Brain delegation**: none. This manager makes no value-judgement or scoring decision and delegates nothing into `machine_learning/`. It is pure FETCH + reshape. The fields it produces (`in_library`, `last_watched`, `episodes_watched`, `lists`) are raw signals that downstream consumers/brains may use, but no decision is made here. Note that `in_library` and `title` are seeded but never populated within this file — they are placeholders for a consumer to fill.

## Criteria & examples

The only "rules" here are structural extraction guards, not value thresholds:

- **Slug guard** — a list with no `ids.slug` is skipped entirely. Example: a list whose payload is `{"name": "Untitled", "ids": {}}` contributes nothing to the index.
- **Show/TVDB guard** — a list item is only counted if it has both `item["show"]` and `item["show"]["ids"]`, and the extracted `tvdb` is truthy. Example: a list of 10 items where 3 are movies (no `"show"` key) and 1 show is missing a `tvdb` ID yields 6 TVDB IDs.
- **History merge** — `episodes_watched` is the raw count of episode entries. Example: if `get_history_grouped_by_series()` returns `{121361: [ep, ep, ep]}` for *Game of Thrones* (TVDB 121361), then that show's record gets `episodes_watched = 3`.
- **`last_watched` parsing** — taken as the lexicographic `max` of ISO-8601 `watched_at` strings (which sorts correctly for ISO-8601), with `Z` normalized to `+00:00`. Example: episodes watched at `2024-01-02T20:00:00Z` and `2024-01-05T21:30:00Z` produce `last_watched = datetime(2024, 1, 5, 21, 30, tzinfo=UTC)`. A malformed string like `2024-13-99` is swallowed and leaves `last_watched = None`.

## In plain English

Think of your Trakt account as having a few hand-made shelves: "Comfort Rewatches", "To Finish", "Kids Saturday Morning". This manager walks every shelf, writes down which shows are on each one, and then cross-checks that against your actual watch log. The result is a single tidy card per show: which shelves it sits on, how many episodes you've watched, and when you last watched it. For example, *Bluey* might come back as "on the 'Kids Saturday Morning' shelf, 47 episodes watched, last watched two days ago." It doesn't decide anything — it just gathers the facts neatly so the rest of the app can later decide whether to keep, recommend, or tidy that show away.

## Interactions

- **Parent manager**: `TraktManager` (constructs/owns this manager and supplies `trakt_api`; this class inherits its logger/config/cache/validator).
- **Sibling submanagers**: `TraktAPIManager` (`self.trakt_api`, used for every HTTP call via `_make_request`) and its `TraktHistoryManager` child (`trakt_api.history`, used for `get_history_grouped_by_series`).
- **Optional**: `sonarr_api` (accepted but currently unused in this file).
- **Brain modules**: none — no `machine_learning/` delegation occurs here.
- **External service**: the Trakt API (read-only).
