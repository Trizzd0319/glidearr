# TautulliManager

- **File** — `scripts/managers/services/tautulli/__init__.py`
- **One-liner** — Top-level service manager that drives a full Tautulli data-collection run: pulls users, paginated watch history, metadata, libraries, then derives household-completion / affinity / device-codec signals and writes them to the global cache for Radarr/Trakt to consume.

## What it does (for a senior Python engineer)

`TautulliManager(BaseManager, ComponentManagerMixin)` is one of the top-level service managers constructed directly by `Main` in `scripts/main.py` (lines 80–89, immediately before Trakt and Radarr). It is the orchestrator for everything Tautulli-derived. It owns a `TautulliAPI` client and a set of submanagers, and its `run()` method is the single entry point for the Tautulli phase of a pipeline run.

**Manager-tree position.** Parent in the runtime tree is `Main`. `parent_name = "TautulliManager"`, which it also passes down as the `parent_name` of every submanager it loads (so the submanagers auto-link back to it via the shared BaseManager parent-link mechanism). It is a process-wide singleton like every BaseManager.

**Submanagers it loads.** Rather than `ComponentManagerMixin.load_components`, this class uses `split_components(...)` (`scripts/support/utilities/managers/component_splitter.py`) to partition its declared component classes into `critical_components` and `noncritical_components`, then lazily instantiates them through `BaseManager._singleton` inside `_load_component`. The component classes (all in sibling subdirectories — each its own separate doc) are:

| Attribute (`self.…`) | Class | Critical? |
|---|---|---|
| `devices` | `TautulliDevicesManager` | yes |
| `episodes` | `TautulliEpisodesManager` | yes |
| `instance` | `TautulliInstanceManager` | no |
| `metadata` | `TautulliMetadataManager` | yes |
| `series` | `TautulliSeriesManager` | yes |
| `transcode` | `TautulliTranscodeManager` | yes |
| `users` | `TautulliUsersManager` | yes |
| `watch_history` | `TautulliWatchHistoryManager` | yes |
| `validator_manager` | `TautulliValidatorManager` | no (stub) |

`critical_keys = {watch_history, users, metadata, episodes, series, transcode, devices}`. `prepare()` eagerly loads exactly these seven and logs a one-line summary; the non-critical `instance` and `validator_manager` are loaded lazily on first access.

**API client.** In `__init__` it normalises the `tautulli` config block (accepts either a flat `{"url":…, "api":…}` shape or a multi-instance `{"default": {...}}` shape — if the values are not all strings it pulls the `"default"` entry, falling back to the first value) and constructs `TautulliAPI(logger=…, instance_config=tautulli_cfg)`. `TautulliAPI` is re-exported through `tautulli/api.py` from the canonical `tautulli/instances/api.py`.

**FETCH / CACHE / APPLY.** This is a read-only collector — **FETCH** and **CACHE** only, no **APPLY**. It issues HTTP GETs against Tautulli (via the submanagers and the API client) and persists derived results to the global cache. It never PUTs/DELETEs/POSTs anything, so there is no `dry_run`-gated mutation here (the actual Tautulli HTTP endpoints are hit by the API layer / submanagers, e.g. `get_server_info` for the reachability probe).

**Endpoints touched (directly).** `self.api.get_server_info()` in `_is_reachable()` (Tautulli `/api/v2?cmd=get_server_info`). All other endpoint traffic is delegated to submanagers.

**Config keys read.**
- `tautulli` — instance connection block (url / api key, flat or multi-instance).
- `rating_groups` — household grouping for movie-completion aggregation; **defaults to `{"household": {}}`** when unset so the tmdb-completions map is always built.

**global_cache keys written.**
- `tautulli/device_codec_matrix` — per-device codec play/transcode matrix (keystone signal for per-device profile selection; currently no consumer reads it).
- `tautulli/users/<safe_username>/affinity` — per-user genre/actor/director affinity (username sanitised: `[\\/:*?"<>|]` → `_`).
- `tautulli/affinity` — household-wide genre/actor/director affinity (consumed by Radarr/Trakt).
- `tautulli/group/<group_name>/tmdb_completions` — per-group max movie-completion % keyed by `tmdb_id` (consumed by Radarr ratings / space-pressure and the owned-movie watched-set).
- `tautulli/debug/group/<group_name>/unresolved_rating_keys` — diagnostic dump of rating_keys that could not be mapped to a tmdb_id (split into `not_in_metadata` vs `no_tmdb_guid`).

**global_cache keys read.**
- `radarr.movies.standard.full` — used as a fallback bridge to resolve a Plex item to a `tmdb_id` (by imdb id, or by title+year) when Tautulli metadata lacks a tmdb GUID. Guarded by `registry.get("manager", "RadarrManager")`.

Note: the docstring in `tautulli/validator.py` claims `TautulliManager` is commented out of `main.py`; that is stale — `main.py` actively constructs and runs it.

## How it functions

**Lifecycle.**
1. `__init__` — calls `BaseManager.__init__` (injects logger/config/global_cache/validator/registry, self-registers, parent-links), normalises the `tautulli` config block, builds the `TautulliAPI`, assembles `init_args` (the shared dep bundle, including `tautulli_api=self.api`), declares `component_dependencies` (all empty — no inter-component ordering), `all_component_classes`, and `critical_keys`, then runs `split_components(...)` to bucket the classes. Logs `[TautulliManager] initialized`.
2. `prepare()` — eagerly instantiates the seven critical submanagers via `_load_component`, then logs a single status line such as `[TautulliManager] ✅ 7/7: watch_history✅ users✅ …`. Called by `Main` before `run()`.
3. `run()` — the data-collection control flow (below).

**`_load_component(name, …)`** — idempotent lazy loader: returns an already-set attribute; else checks the registry for an existing manager of that name; else looks the class up in `critical_components`/`noncritical_components`, recursively loads declared deps (none are declared), and instantiates via `self._singleton(name, component_class, **self.init_args)`. Records `✅`/`❌`/`❌ unknown` in `self.load_summary` and never raises — a failed component is logged and returns `None`.

**`_is_reachable()`** — short-circuits the whole run: returns `False` (with a warning) if there is no API key or if `get_server_info()` returns falsy, so an unconfigured/unreachable Tautulli is skipped cleanly rather than erroring.

**`run()` control flow** (after the reachability gate):
1. **Users** — `users.get_all_users()` (real-time).
2. **Watch history** — `watch_history.get_all_history_cached()` (paginated, cached ~24 h). This is the master entry list every later step is derived from.
3. *(removed)* per-user watch-time/player-stats fetch — was dead work (debug-logged only).
4. **Metadata index** — distinct `rating_key`s from history → `metadata.get_metadata_index_cached(rating_keys)` (cached ~7 d, one API call per unique key).
5. **Library list** — `metadata.get_library_index()` (real-time).
6. *(removed)* play-statistics aggregation — discarded results, removed.
7. **Derived stats (pure computation over the cached history, no extra API calls)** — `transcode.get_transcode_stats`, `devices.get_platform_usage`, `transcode.get_device_codec_matrix`, `series.get_series_completion_stats`, `episodes.get_episode_completion_stats`. The device-codec matrix is the only one of these persisted here (`tautulli/device_codec_matrix`).
8. **Affinity** — household `users.compute_genre_affinity(...)` and per-user `users.compute_per_user_genre_affinity(...)`, written to `tautulli/affinity` and `tautulli/users/<safe>/affinity`.
9. **Group movie completions** — for each configured rating group (default `household`), `watch_history.get_group_movie_completions(...)` returns `rating_key → {pct, …}`. The manager resolves each rating_key to a `tmdb_id` and keeps the max `pct` per tmdb_id, writing the result to `tautulli/group/<group>/tmdb_completions` and the unresolved set to the debug key.

**tmdb resolution ladder** (inside the group loop) — for each rating_key: (a) use `metadata_index[rk]["tmdb_id"]` if present; (b) else if metadata is missing entirely → `not_in_metadata` bucket (item deleted/replaced in Plex); (c) else skip `iva://` / `local://` GUIDs silently (trailers/extras); (d) else try the imdb→tmdb bridge by matching the item's `imdb://` guid against `radarr.movies.standard.full` (`imdbId`); (e) anything still unresolved goes to `no_tmdb_guid` with its title/year/guids for manual investigation.

**Delegation to a brain.** This manager performs no value-judgement itself — it FETCHes and CACHEs raw and lightly-derived signals. The decisions that *consume* these caches (e.g. owned-movie monitor policy, space-pressure deletion, scoring) live in `machine_learning/` and are invoked by the Radarr/Sonarr managers downstream, not here. (Per project rules the brain itself is out of scope and not documented.)

## Criteria & examples

- **Reachability gate.** No `tautulli.api` key → warn and return immediately (no run). API key present but `get_server_info()` falsy → warn with `base_url` and return.
- **Config-shape detection.** `tautulli = {"default": {"url": "...", "api": "..."}}` → the manager detects a non-all-string mapping and uses the `"default"` sub-dict. `tautulli = {"url": "...", "api": "..."}` (all-string values) → used as-is, flat.
- **Default rating group.** If `rating_groups` is unset/empty, it becomes `{"household": {}}`, so a single household-wide completion map is always produced. A memberless `household` group counts every user (handled in `get_group_movie_completions`).
- **Max-completion-per-tmdb rule.** If three Plex `rating_key`s all map to tmdb `27205` with `pct` values `0.42`, `0.95`, `0.10`, the stored `tmdb_completions[27205]` keeps the `0.95` entry (`data["pct"] >= existing["pct"]`). Those three keys also contribute `duplicate_rk_count = 2` (resolved keys minus unique tmdb ids).
- **GUID filtering.** A rating_key whose metadata guid is `iva://...` or `local://...` is skipped silently (never counted as unresolved) — those are trailers/extras, never real movies.
- **imdb bridge example.** A deleted-but-metadata-present item with guid `imdb://tt0133093` and no tmdb guid: the manager scans `radarr.movies.standard.full` for `imdbId == "tt0133093"`; on a hit it adopts that movie's `tmdbId`; on a miss it lands in `no_tmdb_guid`.

## In plain English

Think of Tautulli as the security camera over your home Plex TV: it records who watched what, how far they got, and on which device. `TautulliManager` is the person who reviews that footage after each run and writes up a few tidy notebooks: a list of everyone who watched, a "how much of each movie did the household actually finish" sheet, a "what genres/actors this household loves" sheet, and a "which devices struggle to play which video formats" sheet. It does not change anything in your library — it only reads and summarises. Those notebooks are then handed to the parts of the app that *do* make decisions (e.g. "we all finished *The Princess Bride* to 95%, so it counts as watched and Radarr can let it go under space pressure"). If the camera is unplugged (Tautulli unreachable or no key), the reviewer just shrugs and skips the write-up rather than crashing the whole run.

## Interactions

- **Parent manager:** `Main` (`scripts/main.py`) — constructs it, calls `prepare()` then `run()`.
- **Sibling submanagers (loaded by this class, each documented separately):** `TautulliUsersManager`, `TautulliWatchHistoryManager`, `TautulliMetadataManager`, `TautulliSeriesManager`, `TautulliEpisodesManager`, `TautulliTranscodeManager`, `TautulliDevicesManager`, plus non-critical `TautulliInstanceManager` and the `TautulliValidatorManager` stub.
- **API client:** `TautulliAPI` (via `tautulli/api.py` → `tautulli/instances/api.py`).
- **Other services:** reads `radarr.movies.standard.full` from the global cache (guarded by `RadarrManager` being registered) for tmdb fallback resolution; writes caches (`tautulli/affinity`, `tautulli/group/*/tmdb_completions`, etc.) consumed downstream by Radarr and Trakt.
- **Brain modules:** none invoked directly; the value-judgements that consume its caches live in `machine_learning/` and run in the Radarr/Sonarr phases.
