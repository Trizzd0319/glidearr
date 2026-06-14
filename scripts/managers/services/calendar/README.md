# CalendarManager

**File** — `scripts/managers/services/calendar/__init__.py`
**One-liner** — Pulls the user's Trakt calendars (upcoming episodes, premieres, movie releases) into cache — plus the MAL seasonal chart gated by watchability — then makes sure any of those titles already in the Sonarr/Radarr library are flagged `monitored` so the *arrs grab them on air.

> **MAL upcoming** (`calendar.mal`, default on when MAL is configured): the current MAL seasonal chart is filtered by the pure `mal_upcoming_above_threshold` — `score_show` on the signals an unowned upcoming anime actually has (genres × household genre affinity from `tautulli/affinity`, plus the MAL community `mean` as the rating) — keeping entries at/above `calendar.mal_min_watchability` (0–100, default 20; unowned entries realistically score 0–25 because the household-intent groups are all 0). Passing entries are cached at `mal/{user}/calendar/upcoming`, and when `ensure_monitored` is on, ones already in the library (exact normalized-title match — fuzzy matching is deliberately avoided) are ensured monitored via the same `_ensure` path as the Trakt titles (TV → Sonarr/tvdbId, `media_type=="movie"` → Radarr/tmdbId). Monitoring flips only — never adds.

## What it does (for a senior Python engineer)

`CalendarManager(BaseManager, ComponentManagerMixin)` is a thin service-tier manager. It is constructed by `Main` (in `scripts/main.py`) as part of the Phase-3 acquisition family and runs after the core Sonarr/Radarr/Trakt managers, since it depends on their authenticated API objects and instance managers being available. Despite mixing in `ComponentManagerMixin`, it does **not** call `load_components` — it has no submanagers; it leans on injected sibling service managers instead.

Injected dependencies (read from `kwargs` in `__init__`):
- `self.trakt` — the Trakt service manager; the calendar reads HTTP through its `trakt_api` attribute.
- `self.sonarr` / `self.radarr` — the *arr service managers; only their `.instance_manager` is used (handed to an `ArrGateway`).
- `self.dry_run` — taken from `kwargs["dry_run"]`, falling back to the parent manager's `dry_run`, else `False`.

It self-registers via `self.register()` and inherits the shared logger/config/global_cache/validator/registry from `BaseManager`.

**Verbs performed:** FETCH (Trakt calendar GETs + an *arr library GET per instance), CACHE (writes the three calendar payloads to `global_cache`), and APPLY (PUTs `monitored=True` and optionally POSTs a search command).

**Public methods:**
- `prepare(self) -> None` — no-op; present to satisfy the manager lifecycle contract.
- `run(self) -> None` — the entry point. Reads `config["calendar"]`, short-circuits unless `calendar.enabled` is truthy, fetches the three Trakt calendars, caches them, then (unless `calendar.ensure_monitored` is `False`) collects the upcoming TVDB/TMDB id-sets and calls `_ensure` for each service.

**Trakt API endpoints touched** (via `self.trakt.trakt_api._make_request`, with `start = today` as `YYYY-MM-DD` and `days` defaulting to 33):
- `calendars/my/shows/{start}/{days}` — upcoming episodes of shows already on the user's Trakt list.
- `calendars/my/shows/premieres/{start}/{days}` — season/series premieres.
- `calendars/my/movies/{start}/{days}` — upcoming movie releases.

**Config keys read:**
- `calendar.enabled` (bool, master switch — no-op when falsy).
- `calendar.days` (int, default `33`).
- `calendar.ensure_monitored` (bool, default `True`).
- `calendar.search` (bool, default `False`) — whether to also trigger an *arr search, matching the conservative acquisition policy.
- `trakt.username` (default `"default"`) — only used to build the cache key.
- `sonarr_instances` / `radarr_instances` — enumerated by `_instance_names` to find concrete instance names (skipping the `default_instance` pointer key and any non-dict values).

**global_cache keys written** (the long-declared-but-previously-dormant `trakt/<user>/calendar/*` keys; `<user>` = `trakt.username`):
- `trakt/<user>/calendar/shows`
- `trakt/<user>/calendar/shows/premieres`
- `trakt/<user>/calendar/movies`

No Parquet I/O. No global_cache reads.

**dry_run behavior:** when `self.dry_run` is `True`, the APPLY steps log a "would monitor"/"would search" line and mutate nothing; the calendar fetch + cache writes still happen, and the per-service summary line reports "would-monitor" counts.

**Singleton / concurrency:** inherits the `BaseManager` process-wide singleton behavior (cached in `_instances` by `(class, singleton_key)`). `run()` is synchronous and single-threaded; the only memoisation is inside the per-run `ArrGateway` instances it creates.

## How it functions

Lifecycle: `__init__` injects deps and registers → `prepare()` (no-op) → `run()`.

Inside `run()`:
1. Load the `calendar` config block; bail with a debug log if not `enabled`.
2. Grab `self.trakt.trakt_api`; bail with a warning if absent.
3. Compute `start`/`days`, issue the three calendar GETs (each falling back to `[]`).
4. Resolve `user` from `trakt.username` and write all three payloads to `global_cache`; log a one-line summary of counts.
5. If `ensure_monitored` is disabled, return.
6. Build `up_tvdbs` (from `shows + premieres`) and `up_tmdbs` (from `movies`) using the `_gid` extractor, dropping `None`s, then call `_ensure("sonarr", "tvdbId", …)` and `_ensure("radarr", "tmdbId", …)`.

Internal helpers:
- `_gid(item, kind, idk)` — static; safely digs `item[kind]["ids"][idk]` out of a Trakt calendar row and stringifies it (or returns `None`). E.g. `_gid(row, "show", "tvdb")`.
- `_instance_names(service)` — returns concrete instance keys from `config["<service>_instances"]`, excluding `default_instance` and non-dict entries.
- `_ensure(service, id_field, upcoming_ids, search)` — the APPLY core. Returns early if there are no upcoming ids. Constructs an `ArrGateway(service, <service>.instance_manager, config, logger)`; returns if the gateway is not `available`. Iterates each instance (or the gateway's `default_instance()` if none enumerated), pulls `gw.library_items(inst)` (a memoised `GET series`/`GET movie`), and for each library item whose `id_field` matches an upcoming id:
  - if it is not already `monitored`, PUTs the full item dict with `monitored=True` to `series/{id}` (Sonarr) or `movie/{id}` (Radarr) — or logs a dry-run line — and increments `monitored`;
  - if `search` is enabled and the item has an id, POSTs a `command` (`{"name": "SeriesSearch", "seriesId": id}` for Sonarr, `{"name": "MoviesSearch", "movieIds": [id]}` for Radarr) — or logs a dry-run line — and increments `searched`.
  Finishes with a per-service summary log.

The actual *arr HTTP (library GET, monitor PUT, search command POST) is delegated to `ArrGateway` (`scripts/managers/services/acquisition/gateway.py`), which memoises the full-library snapshot per instance for the run.

**Brain delegation:** none. `CalendarManager` makes no value judgement that it routes into `machine_learning/`. Its only "decision" is the purely mechanical id-set intersection and the `monitored`/`search` flags from config; there is no scoring or ranking here.

## Criteria & examples

- **Master gate:** `calendar.enabled` must be truthy or `run()` logs `"[Calendar] disabled — skipping."` and returns. Example: with `calendar` absent or `{"enabled": false}`, nothing is fetched or cached.
- **Window:** `days` defaults to 33. Example: run on `2026-06-10` issues `calendars/my/shows/2026-06-10/33`, covering through ~mid-July.
- **Monitor only what's owned:** a title is acted on only if its id (`tvdbId` for Sonarr, `tmdbId` for Radarr) appears in **both** the upcoming calendar id-set **and** the *arr library. Example: an upcoming episode of a show with `tvdb=121361` is monitored only if a Sonarr series with `tvdbId == "121361"` already exists; a brand-new title on the calendar but not in the library is ignored (this manager never *adds*, only monitors/searches existing items).
- **Skip already-monitored:** an in-library matching item that already has `monitored == True` is not PUT again (not counted in `monitored`). Example: a series already monitored contributes 0 to the monitored count even if it appears in the calendar.
- **Search is opt-in:** `calendar.search` defaults to `False`, so by default the manager only flips the monitored flag and lets the *arr's own RSS/scheduling grab the file on air. Example: with `search=true`, a newly-monitored Radarr movie with `id=842` triggers `POST command {"name":"MoviesSearch","movieIds":[842]}`.
- **ensure_monitored off:** with `calendar.ensure_monitored == false`, the calendars are still fetched and cached but no `_ensure` pass runs.
- **dry_run:** with `dry_run=true`, an item needing monitoring logs `"[Calendar] dry_run — would monitor sonarr '<title>'"` and is counted as would-monitor, but no PUT/POST is sent.

## In plain English

Think of this as setting a DVR reminder for shows and movies you already follow. Suppose you have *Stranger Things* in your library and a new season is dropping in three weeks. Trakt knows the air date is coming up; this manager reads that calendar, notices *Stranger Things* is already in your Sonarr library, and quietly flips its "record this" switch on so the new episodes get pulled down automatically the night they air — no scrambling to remember. By default it only arms the recorder and waits for the broadcast; if you turn on the optional "go fetch it now" setting, it will also kick off an active search. It never adds shows you don't already own — it only makes sure the ones you do are ready to catch what's coming.

## Interactions

- **Parent manager:** `Main` (`scripts/main.py`), which constructs it after the Sonarr/Radarr/Trakt managers and injects them plus `dry_run`.
- **Sibling service managers it talks to:** the Trakt manager (`scripts/managers/services/trakt/`) via its `trakt_api._make_request` for calendar GETs; the Sonarr and Radarr managers via their `.instance_manager`, wrapped in `ArrGateway`.
- **Helper:** `ArrGateway` (`scripts/managers/services/acquisition/gateway.py`) for all *arr reads/writes.
- **Shared infra:** `GlobalCacheManager` (cache writes), `ConfigManager` (config reads), `RegistryManager` (self-registration), `LoggerManager`/`@timeit` (logging + timing).
- **Brain modules:** none — this manager performs no `machine_learning/` delegation.
