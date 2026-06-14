# AcquisitionManager

- **File** — `scripts/managers/services/acquisition/__init__.py`
- **One-liner** — The final-phase orchestrator that turns Trakt recommendations/watchlist (and MAL anime) into monitored *arr adds, gated by an enable flag, `dry_run`, and disk-space pressure.

## What it does (for a senior Python engineer)

`AcquisitionManager(BaseManager, ComponentManagerMixin)` is a process entry-phase manager constructed by `Main` in `scripts/main.py` (right after Sonarr/Radarr, in the "Phase-3 capabilities" block). It is the consumer side of the recommendation pipeline: the app already *fetches* Trakt recs/watchlist elsewhere; this manager finally *acts* on them — gather → dedup-against-library → score → resolve instance/profile/root/size → add **monitored with search OFF**.

It does NOT call `load_components`. Despite mixing in `ComponentManagerMixin`, it does not register submanagers; instead `run()` directly instantiates five plain (non-`Manager`) helper objects each run: `ArrGateway` (×2, one per service), `CandidateGatherer`, `Resolver`, `AcquisitionScorer`, and `Adder`. These live in sibling modules in the same directory and are documented in the "Interactions" section but are out of the Manager-doc scope.

Place in the manager tree:
- **Parent:** `Main` (`scripts/main.py`). `Main` injects `dry_run`, `trakt`, `mal`, `sonarr`, `radarr` plus the standard `logger`/`config`/`global_cache`/`validator`/`registry`. The constructor reads `dry_run` from kwargs, falling back to the parent manager's `dry_run`, else `False`.
- **Submanagers loaded via `load_components`:** none.
- Self-registers under the registry ("manager" category) via `self.register()`; `Main` sets the registry flag `acquisition_initialized` after construction.

FETCH / CACHE / APPLY profile:
- **FETCH** — through `ArrGateway`: library id-sets (GET `series`/`movie`), quality profiles (GET `qualityprofile`), root folders (GET `rootfolder`), quality definitions (GET `qualitydefinition`), and metadata lookup (GET `series/lookup?term=` / `movie/lookup?term=`). Candidate fetch is via the injected `trakt`/`mal` services (Trakt watchlist/recommendations, MAL `acquisition_candidates()`).
- **APPLY** — adds (POST `series` / `movie`) and deferred searches (POST `command` with `SeriesSearch` / `MoviesSearch`). All writes route through `ArrGateway`, which wraps each service's `instance_manager._make_request`.
- **CACHE** — writes the deferred-search backlog to `global_cache["acquisition/deferred_search"]` and run stats to `global_cache["acquisition/run_stats"]`. Reads the same deferred key; the scorer additionally reads `global_cache["tautulli/affinity"]`.

External API endpoints touched (all relative to a resolved Sonarr/Radarr instance via `_make_request`):
- `GET series` / `GET movie` — full library (dedup; rides the run-scoped instance snapshot cache).
- `GET qualityprofile`, `GET rootfolder`, `GET qualitydefinition`.
- `GET series/lookup?term=…` / `GET movie/lookup?term=…`.
- `POST series` / `POST movie` — the add.
- `POST command` — `{"name":"SeriesSearch","seriesId":…}` or `{"name":"MoviesSearch","movieIds":[…]}` for deferred searches.

Config keys read (the `acquisition` block unless noted):
- `acquisition.enabled` — master gate; `run()` no-ops (debug log) when false.
- `acquisition.sources` (`trakt_watchlist`, `trakt_recommendations`, `mal`; all default true) — passed to `CandidateGatherer`.
- `acquisition.recommendation_limit` (default 20) — Trakt recs page size.
- `acquisition.monitored` (default true), `acquisition.search_on_add` (default false) — passed to `Adder`.
- `acquisition.defer_under_pressure` (default true) — gate for the new-add deferral path.
- `acquisition.min_score` (default 0), `acquisition.max_adds_per_run` (default 10; ≤0 = unlimited).
- `acquisition.quality_profile` — optional pinned profile name (handled by `Resolver`).
- Disk-space gating reads via `space_targets(self.config, total_gb=…)` and `alert_unconfigured_floor(...)` (e.g. `free_space_limit`; falls back to 25%-of-total when unset). The `Resolver`/`AcquisitionScorer` read additional library-routing/affinity keys (documented in their own modules).

`global_cache` keys read/written:
- `acquisition/deferred_search` — list of deferred backlog items (read + written; capped at `_DEFERRED_MAX = 500`, keeping the newest).
- `acquisition/run_stats` — per-run stats dict (written; failures swallowed).
- `tautulli/affinity` — read by the scorer for genre-affinity weights.

`dry_run` behavior:
- Adds become "would-add" log lines; nothing POSTed (handled in `Adder.add`).
- Deferred-search flush logs "would search deferred …" and counts it as searched, but issues no command and does NOT rewrite the backlog (`global_cache.set` is skipped under dry_run).
- New deferrals are NOT persisted under dry_run (the `if res.get("ok") and not self.dry_run` guard).

Singleton / concurrency: `BaseManager` is a process-wide singleton keyed by `(class, singleton_key)`. `run()` is single-threaded; the only "concurrency" concern is the parallel Radarr/Sonarr/Trakt auth check that `Main` performs *before* this manager is constructed.

## How it functions

Lifecycle: `__init__` (inject deps, capture `dry_run`/`trakt`/`mal`/`sonarr`/`radarr`, `register()`) → `prepare()` is a no-op → `run()` is the entry point.

`run()` control flow:
1. Read the `acquisition` config block; bail with a debug log if `enabled` is falsy.
2. Build two `ArrGateway`s (`sonarr`, `radarr`) from `self.sonarr.instance_manager` / `self.radarr.instance_manager`. Build `CandidateGatherer`, `Resolver`, `AcquisitionScorer`, and `Adder`.
3. **Always flush the deferred backlog first** via `_flush_deferred(...)` — this is idempotent and space-gated, and runs even when `defer_under_pressure` is disabled, so titles already added-but-unsearched are never stranded.
4. `gatherer.gather()` → raw candidates (Trakt watchlist gathered before recommendations so stronger intent wins de-dup).
5. For each raw candidate: `resolver.prepare(cand)`; if it returns a `skip_reason` (e.g. "no instance available", "no lookup match", "already in library") tally it and skip. Otherwise `scorer.score(enriched)` sets `score` and `matrix`, then `resolver.resolve_quality(enriched, score)` re-picks the quality profile from the score (no-op if a profile is pinned).
6. Filter to `score >= min_score`, sort by score descending, truncate to `max_adds_per_run`.
7. For each selected item: determine the target service (`show`→sonarr, else radarr), and if `defer_under_pressure` and the gateway exists, compute the space band via `_space_band(...)` to set `under_pressure = free < U`. Call `adder.add(e, search=False if under_pressure else None)`. If under pressure and the result is "added"/"would-add", relabel the decision "deferred", and (live runs only) queue a backlog entry containing service/instance/`arr_id`/title/type/profile/`queued_at`/`attempts`.
8. Persist new deferrals onto `acquisition/deferred_search` (bounded to the newest 500), render the decision table (`title, type, score, instance, profile, ~size, decision`), log skip tallies, and write `acquisition/run_stats`.

Notable internal helpers:
- `_space_band(gw, inst, cache)` — returns `(free_gb, U)` for an instance. **Fail-open:** an unreadable instance yields `free=inf` (so `free < U` is False and a transient error never blocks an add). Memoised per `(service, instance)` for the run. `U` is the band top from `space_targets` (floor + headroom, or 25%-of-total when `free_space_limit` is unset). Warns once per service+instance when the floor is being defaulted.
- `_trigger_search(gw, inst, item)` — POSTs the deferred search. Returns True only on a truthy *arr response; a falsy/None result (the `_make_request` swallowed-error fallback) means the command failed and the item stays queued.
- `_flush_deferred(gateways, band_cache)` — drains the backlog: items on unavailable/still-pressured instances stay queued (no attempt counted); items whose instance recovered above `U` are searched; an attempted-but-failed search increments `attempts` and is abandoned after `_DEFERRED_MAX_ATTEMPTS = 5`. Returns a stats dict (`pending/searched/abandoned/still_deferred`).
- `_size_str(e)` — formats `~{gb}GB` (with `/ep` suffix for per-episode shows) for the decision table.

Brain delegation: this manager delegates **no** decision into `machine_learning/`. The acquisition score is computed by the local sibling `AcquisitionScorer` (a service module, not a brain module). The only "value judgement" externalized is disk-space banding, which goes to the shared `space_targets` utility, not the brain.

## Criteria & examples

- **Master gate:** `acquisition.enabled` false → `run()` returns immediately. Example: with `enabled=false`, zero candidates are fetched and nothing is logged beyond a debug line.
- **Min-score filter + cap:** `min_score=40`, `max_adds_per_run=2`. Five candidates score `[82, 61, 55, 38, 12]`. The 38 and 12 are dropped (`< 40`), leaving `[82, 61, 55]` sorted; the cap keeps the top 2 → `82` and `61` are added, `55` is not added this run.
- **Already-in-library dedup:** a watchlist movie with `tmdbId=603` is skipped with reason "already in library" if `603` is in the Radarr instance's id-set; tally `{"already in library": 1}`.
- **Space-pressure deferral:** instance band top `U=550 GB`, current free `free=512 GB` (`512 < 550` → under pressure). A selected movie is still **added monitored at its resolved profile but with search OFF**, the decision is logged as "deferred", and a backlog entry `{service:"radarr", arr_id:…, attempts:0, queued_at:…}` is queued. A later run where free has recovered to `601 GB` (`601 >= 550`) flushes it: `_trigger_search` POSTs `MoviesSearch` and, on a truthy response, the item leaves the queue.
- **Fail-open guard:** if `disk_free_gb` raises, `_space_band` returns `free=inf`; `inf < 550` is False, so the add proceeds with normal search behavior rather than being blocked by a transient error.
- **Deferred retry budget:** an item whose `SeriesSearch` keeps returning empty (e.g. its `arr_id` was deleted) increments `attempts` each flush; on reaching `attempts >= 5` it is abandoned with a warning and dropped from the backlog, so a doomed command can't re-POST forever.
- **Backlog cap:** under chronic pressure the backlog is trimmed to the newest 500 entries (`_DEFERRED_MAX`) every time new deferrals are persisted.

## In plain English

Think of this as the part of the app that finally *goes shopping* for the shows and movies you've said you want. All the recommendations and your watchlist (say, you flagged *The Princess Bride* and Trakt suggested a few Marvel films) have been sitting in a list; this manager checks each one against what you already own, scores how much you'll probably enjoy it, decides which library and quality to use, and then tells Sonarr/Radarr to add it — but quietly, without immediately starting a big download.

The clever part is the "wait until there's room" rule. If your drive is getting full, it still *adds* the title to your list (so you don't lose track of it) but holds off on downloading it. Once you free up space, a later run notices and quietly kicks off the download. And if it's in "dry run" mode, it just tells you "I would add *The Princess Bride*" without actually touching anything — so you can preview every decision before it commits.

## Interactions

- **Parent manager:** `Main` (`scripts/main.py`) — constructs it after Sonarr/Radarr in the Phase-3 block, injecting `trakt`, `mal`, `sonarr`, `radarr`, and `dry_run`.
- **Sibling helper objects (same directory, instantiated per `run()`, not registered submanagers):**
  - `ArrGateway` (`gateway.py`) — cached HTTP access to a Sonarr/Radarr instance (library, profiles, root folders, quality defs, lookup, add, command).
  - `CandidateGatherer` (`candidates.py`) — gathers + normalizes + dedups Trakt/MAL candidates.
  - `Resolver` (`resolver.py`) — looks up canonical metadata, classifies into a library bucket (delegating to the shared `library_classifier`), picks instance/profile/root folder, and estimates size via the shared `size_model`.
  - `AcquisitionScorer` (`scorer.py`) — explainable 0–100 score with a per-component matrix (genre affinity, source intent, rating, popularity, recency).
  - `Adder` (`adder.py`) — builds the Sonarr v4 / Radarr add payload and POSTs it (or logs "would-add" under dry_run).
- **Services it talks to:** Sonarr and Radarr (via their `instance_manager`s, through `ArrGateway`), Trakt (`trakt_api` watchlist/recommendations), and MAL (`acquisition_candidates()` once wired).
- **Shared utilities:** `space_targets` and `alert_unconfigured_floor` (disk-space banding), `library_classifier` and `size_model` (via the `Resolver`).
- **Brain modules:** none. Scoring is local to `AcquisitionScorer`; no decision is delegated into `machine_learning/`.
