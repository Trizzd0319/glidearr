# TraktRatingsManager

- **File** — `scripts/managers/services/trakt/ratings/__init__.py`
- **One-liner** — A thin Trakt service adapter that reads the user's existing show/episode/movie ratings and auto-submits new star ratings for content the household has actually watched.

## What it does (for a senior Python engineer)

`TraktRatingsManager(BaseManager, ComponentManagerMixin)` is a submanager of `TraktManager` (`parent_name = "TraktManager"`). It does **FETCH** (GET rating/watched lists), **CACHE** (persists raw rating lists via `global_cache`), and **APPLY** (POSTs new ratings to Trakt) — i.e. it touches all three verbs of the thin-adapter model. It loads no submanagers of its own (it does not call `load_components`); it is itself a leaf in the manager tree.

### Init / dependency wiring
`__init__` calls `super().__init__(...)` (so it inherits the shared logger/config/global_cache/validator/registry from `BaseManager`) and then `self.register()`. It pulls two things off `kwargs`:
- `manager` → the parent `TraktManager` instance.
- `trakt_api` → the Trakt HTTP client used for every request.

It resolves `dry_run` defensively by walking a chain — `kwargs["dry_run"]` → parent's `dry_run` → registry-looked-up `TraktManager.dry_run` → registry-looked-up `Main.dry_run` — and **raises `ValueError` if none resolves** (it never silently defaults to `False`, to avoid accidental destructive POSTs). This is the project's documented "dry_run propagation footgun" handled explicitly rather than relying on `BaseManager`.

It reads `config["trakt"]["username"]` into `self.user` (default `"default"`); this username is the namespace segment in every cache key.

### Public methods
Read side (all FETCH + CACHE):
- `get_rated_shows() -> list` — returns the user's rated shows. When `global_cache` is present, served via `get_or_generate_cache(key="trakt/<user>/ratings/shows", generator=trakt_api._make_request("sync/ratings/shows"))`; otherwise falls back to `_fetch_ratings("shows")`.
- `get_rated_episodes() -> list` — same pattern, key `trakt/<user>/ratings/episodes`, endpoint `sync/ratings/episodes`.
- `get_rated_movies() -> list` — same pattern, key `trakt/<user>/ratings/movies`, endpoint `sync/ratings/movies`.
- `get_user_ratings() -> list` — fetches and concatenates **all** ratings across `shows`, `episodes`, `movies` directly from the API (no cache layer), logging a per-type count and a total. Returns the flat combined list.

Write side (FETCH + APPLY):
- `auto_rate_watched_shows(threshold=0.6, rating=7, min_completed=3, progress_map=None) -> dict` — submits a single integer `rating` (default 7/10) for every show the user has watched `>= threshold` of aired episodes and has not already rated. Returns `{rated, skipped_already_rated, skipped_insufficient, errors}`.
- `auto_rate_watched_movies(movies, completion_map, watched_tmdb_ids, genre_affinity, people_manager=None) -> dict` — scores every household-watched movie via `score_movie` and POSTs the per-movie ratings. Returns `{rated, skipped_already_rated, skipped_no_data, errors}`.

Private helper:
- `_fetch_ratings(media_type) -> list` — uncached `trakt_api._make_request(f"sync/ratings/{media_type}") or []`; returns `[]` if no `trakt_api`.

### External Trakt API endpoints touched
- `GET sync/ratings/{shows|episodes|movies}` — read existing ratings.
- `GET sync/watched/shows` — used by `auto_rate_watched_shows` to obtain the full ID block (`trakt`/`slug`/`tvdb`/`tmdb`) needed to build a ratings POST payload.
- `POST sync/ratings` (`method="POST"`, `data={"shows": [...]}` or `data={"movies": [...]}`) — the APPLY step that writes new ratings.

### Cache keys
- Reads/writes (via `get_or_generate_cache`): `trakt/<user>/ratings/shows`, `trakt/<user>/ratings/episodes`, `trakt/<user>/ratings/movies`.
- **Invalidates** `trakt/<user>/ratings/shows` after a successful (non-dry-run) show-rating POST and `trakt/<user>/ratings/movies` after a successful movie-rating POST, so the next read reflects the new state.
- Reads (by contract, passed in by the caller, not fetched here): `tautulli/group/<group>/tmdb_completions` (becomes `completion_map`) and `tautulli/affinity` (becomes `genre_affinity`).

### Config keys read
- `trakt.username` (only).

### dry_run behavior
When `self.dry_run` is true, both auto-rate methods build the full candidate batch and then log a "would POST N ratings" line plus a per-entry debug line, **mutating nothing** and **not invalidating any cache**. The `rated` count is still computed/returned. When live, they POST and then log Trakt's confirmed `added` / `existing` / `not_found` counts.

### Singleton / concurrency notes
As a `BaseManager`, it is a process-wide singleton keyed by `(class, singleton_key)`. No internal threading; all calls are synchronous HTTP via `trakt_api`. No locks are taken.

## How it functions

Lifecycle: `TraktManager` constructs this manager (passing `manager=self` and `trakt_api`), `__init__` resolves deps + `dry_run` + `self.user`, and that is the whole setup — there is no `load_components` step and no dedicated `run()`; the parent calls individual methods.

`auto_rate_watched_shows` control flow:
1. `GET sync/watched/shows` → build `id_by_slug` (`{slug: ids-block}`).
2. Obtain `progress_map`. If the caller did not pass one, it lazily fetches it from `self.trakt_api.progress.get_combined_progress_watched()` (the `TraktProgressManager`). The docstring explicitly recommends `TraktManager.run()` pass a pre-fetched `progress_map` to avoid a 100+ call refetch.
3. `get_rated_shows()` → `already_rated` set of slugs (so manual ratings are never overwritten).
4. Iterate `progress_map`; apply the completion guards (below) to build `to_rate` as `[{"rating": rating, "ids": ids}, ...]`.
5. If dry-run, log; else POST `{"shows": to_rate}`, parse `added`/`existing`/`not_found`, and invalidate the shows cache.

`auto_rate_watched_movies` control flow:
1. Build `collection_members` (`{collection_tmdb_id: {member tmdbIds}}`) and `movie_by_tmdb` from the Radarr `movies` list passed in.
2. `get_rated_movies()` → `already_rated` set of tmdbIds.
3. Iterate `completion_map`; for each watched movie not already rated and present in Radarr with `pct >= 0.25`, optionally fetch credits via `people_manager.get_people(tmdb_id)`, then call `score_movie(...)` to produce the 1-10 rating.
4. If dry-run, log; else POST `{"movies": payload}`, parse the response, and invalidate the movies cache.

### Delegation note
The per-movie 1-10 score is delegated to `score_movie` in `scripts/managers/services/trakt/movies/scorer.py` (a sibling **service-side** scorer — *not* a `machine_learning/` brain module, and therefore out of scope here; this manager does not itself reach into `machine_learning/`). This adapter only assembles inputs (collection index, watched set, genre affinity, optional credits) and applies the returned integer.

## Criteria & examples

Show auto-rating guards (all must pass, evaluated per show in `progress_map`):
- `aired` must be non-zero.
- `completed >= min_completed` (default 3).
- `completed / aired >= threshold` (default 0.6).
- Slug must not already be in `already_rated`.
- The slug must have an ID block from the watched list.

Worked examples:
- A show with `aired=20`, `completed=15` → ratio `0.75 >= 0.6`, `completed 15 >= 3`, not yet rated → **gets a 7/10**.
- A single-episode pilot with `aired=1`, `completed=1` → ratio is `1.0` (passes threshold) but `completed 1 < min_completed 3` → **skipped as insufficient** (this is exactly the case `min_completed` exists to block).
- A show with `aired=10`, `completed=4` → ratio `0.4 < 0.6` → **skipped as insufficient**.
- A show already in `already_rated` → **counted under `skipped_already_rated`**, never overwritten.

Movie auto-rating guard:
- A movie is skipped (`skipped_no_data`) if its `tmdb_id` is not in `movie_by_tmdb` (not in Radarr) or its completion `pct < 0.25` (an early walk-out). Note: the source comment says "< 50% is an early walk-out" but the actual code floor is `0.25`.
- Example: a movie watched to `pct=0.18` → below the `0.25` engagement floor → **skipped (`skipped_no_data`)**. A movie watched to `pct=0.92`, present in Radarr, not yet rated → scored by `score_movie` and **queued to POST**.

## In plain English

Think of this as the part of the app that quietly fills in your star ratings on Trakt so you don't have to. If you binged 15 of the 20 episodes of *The Mandalorian*, the app notices you clearly liked it (you watched 75%, well past the "you probably enjoyed this" line) and stamps it a 7/10 on your behalf — but only if you finished enough episodes that it's a real opinion and not just one pilot you sampled. For movies, if your household watched most of *The Princess Bride*, it computes a fair star score and records that too. Crucially, it never touches a rating you set yourself, and in "dry run" mode it just tells you what it *would* rate without changing anything. The payoff: your Trakt profile reflects your real taste, which makes every future recommendation smarter.

## Interactions

- **Parent:** `TraktManager` — constructs this manager and is expected to drive `auto_rate_watched_shows` / `auto_rate_watched_movies`, passing in a shared `progress_map`.
- **Siblings / collaborators:** `TraktProgressManager` (`self.trakt_api.progress`, for `get_combined_progress_watched`); `TraktMoviePeopleManager` (the optional `people_manager`, for `get_people`); `score_movie` in `services/trakt/movies/scorer.py` (sibling service-side scorer).
- **Upstream data producers (via cache, passed in by the caller):** Tautulli managers populate `tautulli/group/<group>/tmdb_completions` and `tautulli/affinity`; Radarr supplies the `movies` library list.
- **Shared infra:** `BaseManager`/`RegistryManager` (singleton + parent auto-link + `dry_run` chain resolution), `GlobalCacheManager` (rating-list caching + invalidation), and `Main` (ultimate `dry_run` source).
- **Brain modules:** none — this manager delegates only to a sibling service-side scorer, not to `machine_learning/`.
