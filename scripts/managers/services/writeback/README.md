# WritebackManager

- **File** — `scripts/managers/services/writeback/__init__.py`
- **One-liner** — The final-phase service manager that pushes the local library/watch state *outward* — mirroring the *arr library into a Trakt collection, pushing episode-level watch history to Trakt, and reflecting watch progress to a MyAnimeList account.

## What it does (for a senior Python engineer)

`WritebackManager(BaseManager, ComponentManagerMixin)` is a thin orchestrator that runs in `main.py`'s final phase, after the Sonarr/Radarr/Trakt/Tautulli service managers have already fetched and cached their data. Its job is the reverse direction of the rest of the app: instead of pulling signals *in*, it pushes local state *out* to the external services. It performs **APPLY** work almost exclusively (POSTs/PATCHes to Trakt and MAL); the only reads it does are diff-and-dedup reads to avoid re-pushing items already present remotely.

### Construction / dependency injection
`__init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs)` calls `super().__init__(...)` (the standard `BaseManager` injection of logger/config/global_cache/validator/registry, singleton caching, registry self-registration, parent auto-link) then `self.register()`. It then reads several injected handles from `kwargs`:
- `self.dry_run` — taken from `kwargs["dry_run"]`, falling back to the parent manager's `dry_run`, else `False`. (This is the documented `dry_run`-propagation footgun: each manager must capture it explicitly, which this one does.)
- `self.trakt`, `self.mal`, `self.sonarr`, `self.radarr`, `self.tautulli` — the already-constructed sibling service managers, passed in from `main.py`.

`parent_name` is `"WritebackManager"`.

### Public methods
- `prepare(self) -> None` — no-op (present for interface symmetry with other managers).
- `run(self) -> None` — the entry point. Reads two config blocks and dispatches up to three independently-gated sub-syncs (see below). Each sub-sync is wrapped in its own `try/except` that logs a `log_warning("[writeback] … sync failed: …")` and continues, so one failing sync never aborts the others.

### Submanagers / components
**Notably, this manager does NOT use `ComponentManagerMixin.load_components`** despite inheriting the mixin. Instead, `run()` directly constructs three plain helper classes (each defined in a sibling file in this same directory; none of them is a `*Manager`, so they are out of scope as standalone docs but are summarized here because `WritebackManager` is just a dispatcher over them):

1. **`TraktCollectionSync`** (`trakt_collection.py`) — gated by `trakt_writeback.enabled` AND `trakt_writeback.collection` (default `True`). Unions every Sonarr instance's series (by `tvdbId`) and every Radarr instance's movies (by `tmdbId`) via an `ArrGateway` per service, diffs against the current Trakt collection (GET `sync/collection/shows`, GET `sync/collection/movies`), and POSTs only the missing items to `sync/collection` in chunks of 100. On success it invalidates the `trakt/<username>/collection/shows` global_cache key.
2. **`TraktHistorySync`** (`trakt_history.py`) — gated by `trakt_writeback.enabled` AND `trakt_writeback.history` (default `True`). Paginates raw Tautulli `get_history`, filters to watched plays (`watched_status == 1` OR `percent_complete >= threshold`), resolves external IDs via the `tautulli/metadata/index` global_cache entry, and POSTs episode-level + movie history to `sync/history` (movies chunked 100, shows chunked 50).
3. **`MalListSync`** (`mal_list.py`) — gated by `mal_writeback.enabled`. Builds per-show watched-episode counts from Tautulli history (matched to the MAL list by normalized title), and PATCHes `my_list_status` for shows whose watched count exceeds what MAL records.

### Config keys read
- `trakt_writeback` (object) with sub-keys: `enabled`, `collection` (default `True`), `history` (default `True`), `watched_threshold` (default `85`), `history_max_pages` (default `20`).
- `mal_writeback` (object) with sub-key: `enabled`.
- The sub-syncs additionally read `sonarr_instances` / `radarr_instances` (instance enumeration), `trakt.username` and `mal.username` (cache-key namespacing).

### Global_cache / Parquet keys
`WritebackManager` itself touches no cache directly. Its sub-syncs read `tautulli/metadata/index` (GUID/ID resolution) and `mal/<username>/animelist`, and invalidate `trakt/<username>/collection/shows` after a successful collection push.

### External API endpoints (via sub-syncs)
- Trakt: GET `sync/collection/shows`, GET `sync/collection/movies`, POST `sync/collection`, POST `sync/history`.
- Tautulli: `get_history` (paginated).
- MAL: `get_anime_list`, `update_list_status`.
- Sonarr/Radarr: GET `series` / `movie` (library id-sets, via `ArrGateway`).

### dry_run behavior
Fully honored and threaded down to every sub-sync. When `self.dry_run` is true, each sync logs a `"dry_run — would …"` line with the counts it *would* push and writes nothing — no POST/PATCH and no cache invalidation.

### Singleton / concurrency notes
Standard `BaseManager` process-wide singleton. `run()` executes the three syncs sequentially in the calling thread; no threading or parallelism of its own.

## How it functions

Lifecycle: `main.py` constructs `WritebackManager` last, injecting the live `trakt`/`mal`/`sonarr`/`radarr`/`tautulli` managers plus `dry_run`, then calls `run()`.

Control flow inside `run()`:
1. Read `trakt_writeback` and `mal_writeback` config blocks (defaulting each to `{}`).
2. If `trakt_writeback.enabled`:
   - if `collection` (default on): construct and `.run()` a `TraktCollectionSync(self.trakt, self.sonarr, self.radarr, self.config, self.logger, self.dry_run)`.
   - if `history` (default on): construct and `.run()` a `TraktHistorySync(self.trakt, self.tautulli, self.global_cache, self.config, self.logger, self.dry_run)`.
   - else log `"[Writeback] trakt_writeback disabled — skipping."` at debug.
3. If `mal_writeback.enabled`: construct and `.run()` a `MalListSync(self.mal, self.tautulli, self.global_cache, self.config, self.logger, self.dry_run)`; else log the MAL-disabled debug line.

Each sub-sync `.run()` call is individually `try/except`-guarded; an exception is downgraded to a `log_warning` and the next sync still runs.

No decision in this manager is delegated to a `machine_learning` brain module — write-back is pure mechanical mirroring of already-established local state outward, with no value judgement.

## Criteria & examples

- **Per-sync gating.** `trakt_writeback.collection` and `.history` default to `True`, so enabling `trakt_writeback.enabled` alone turns *both* Trakt syncs on. Setting `trakt_writeback = {"enabled": true, "history": false}` runs only the collection sync. `mal_writeback.enabled` independently controls the MAL sync.
  - *Example:* config `{"trakt_writeback": {"enabled": true, "collection": false}, "mal_writeback": {"enabled": false}}` → only the Trakt history sync runs; collection and MAL are skipped.

- **Watched threshold (history sync).** A Tautulli entry counts as watched if `watched_status == 1` OR `percent_complete >= watched_threshold` (default `85`).
  - *Example:* an episode left at `percent_complete = 80` with `watched_status = 0` is **dropped** (80 < 85); the same episode at 86% is pushed to Trakt history. Raising `watched_threshold` to `95` would also drop the 86% play.

- **Collection dedup.** Only library IDs *not already in* the Trakt collection are pushed. If every Sonarr `tvdbId` and Radarr `tmdbId` is already present, `run()` logs `"Trakt collection already up to date."` and pushes nothing.
  - *Example:* a Radarr library of 500 movies where 498 are already in Trakt → only the 2 new `tmdb` ids are POSTed (in a single chunk, since 2 < 100).

- **MAL status mapping.** For a matched show, MAL status becomes `completed` only when `wc >= total` (with a known total), otherwise `watching`; the pushed episode count is capped at `total`.
  - *Example:* a 12-episode anime where Tautulli shows 12 distinct watched episodes and MAL records 7 → status set to `completed`, `num_watched_episodes = 12`. If Tautulli showed 9 of 12, status `watching` with `num_watched_episodes = 9`. If MAL already records 12 (`wc <= current`), nothing is sent.

- **Failure isolation.** If the collection sync raises (e.g. Trakt API unavailable), the history and MAL syncs still attempt to run; the failure is a single warning line.

## In plain English

Most of this app's machinery *pulls* information in: it asks Trakt, Tautulli, Sonarr and Radarr "what's in my library and what have I watched?" The Writeback manager does the opposite — it *tells* those outside services what you've done at home, so your accounts elsewhere stay in sync without you lifting a finger.

Three things happen. First, every movie and show sitting on your server gets quietly added to your Trakt "collection" — like making sure the list a friend keeps of your DVD shelf actually matches the shelf. Second, when your household finishes an episode of *Bluey* or a *Marvel* film on your media server, that "watched" stamp gets pushed up to Trakt so your watch history is complete. Third, if you track anime on MyAnimeList, finishing all 12 episodes of a series at home automatically flips that title to "completed" on MAL.

Crucially, it only ever *adds* what's missing — it checks first and won't re-send things already recorded — and in "dry run" mode it just says "I would have added these 5 movies" and touches nothing, so you can preview before letting it write.

## Interactions

- **Parent manager** — `Main` (`scripts/main.py`), which constructs it last and injects every sibling service handle plus `dry_run`.
- **Sibling service managers (injected, used as data sources / API clients)** — Trakt (`self.trakt.trakt_api`), Tautulli (`self.tautulli.api`), Sonarr / Radarr (via their `instance_manager` wrapped in `ArrGateway`), and MAL (`self.mal.mal_api`).
- **Helper classes it drives (same directory, not standalone managers)** — `TraktCollectionSync`, `TraktHistorySync`, `MalListSync`, plus the shared utilities in `_util.py` (`fetch_history`, `extract_id`, `iso_utc`, `norm_title`, `chunked`) and `ArrGateway` from `scripts/managers/services/acquisition/gateway.py`.
- **Brain modules** — none; this manager delegates no value judgement to `machine_learning/`.
