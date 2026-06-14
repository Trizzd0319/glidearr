# RadarrSpacePressureManager

**File** — `scripts/managers/services/radarr/quality/space_pressure.py`
**One-liner** — The Radarr disk-space firefighter: when free space drops below the pressure band it upgrades actively-watched movies (if there's surplus), steps low-value movies down toward 720p, and as a last resort deletes the lowest-rated watched/expired movies — all guarded, ledgered, and restorable.

## What it does (for a senior Python engineer)

`RadarrSpacePressureManager(BaseManager, ComponentManagerMixin)` is the heavy orchestrator of the quality subtree. It is FETCH (GET qualityprofile / movie / disk free+total), APPLY (PUT movie profile, POST MoviesSearch, DELETE moviefile), CACHE (writes a restore-set into `global_cache`), and it reads/writes the movie-files Parquet via `RadarrCacheMovieFilesManager`. The *decisions* (which titles to downgrade/upgrade/delete, in what order, and by how much) are delegated to `machine_learning.space.*` brain planners; this manager only computes inputs and APPLIES the returned plan.

Class attributes / tunables:
- `PRESSURE_THRESHOLD_GB = 25.0` — last-resort floor only (used when both `free_space_limit` and the total drive size are unreadable).
- `HD_720P_PROFILE_NAME = "HD-720p"` — the downgrade floor profile.
- `RECENT_WATCH_DAYS = 7`, `COLLECTION_WINDOW_DAYS = 30`, `WATCHABILITY_PROTECT_THRESHOLD = 6`.

Public methods:
- `run(instance) -> dict` — Full pipeline (see How it functions). Bails when `free >= U`.
- `run_active_watcher_upgrades(instance, free_space_gb) -> dict` — Stage 0. Only when `free >= U`: upgrade non-kids, actively-watched movies (watched within `ACTIVE_WATCH_DAYS = 30`, kids certs `{g, pg, tv-g, tv-y, tv-y7}` excluded) to the best profile; never touches keep_universe/keep_forever/keep_movie. Decision = brain `plan_movie_upgrades`.
- `run_downgrades(instance, free_space_gb) -> dict` — Stage 1. Step low-watchability movies DOWN the ranked ladder one rank at a time (floored at the HD-720p resolution) until ~`need_gb` is reclaimed, then PUT each + a single batched `MoviesSearch`. Decision = brain `plan_movie_downgrades`.
- `run_deletions(instance, free_space_gb) -> dict` — Stage 2 (last resort). Target-driven: delete lowest-rated owned movies until projected `free >= U`. Tiered (watched+grace-expired first, then optionally unwatched low-watchability). Records every deletion to a restore-set. Decision = brain `build_movie_delete_candidates`. Skips entirely when the cross-service coordinator owns deletion or when disabled by config.
- `refresh_scores(instance) -> int` — Computes a watchability score (and breakdown) for every Parquet row, writes `watchability_score` + `watchability_breakdown` (JSON) + `watchability_percentile`, and saves (even in dry_run). Must run before the universe manager so it can gate 4K eligibility.
- `build_delete_candidates(instance, df) -> list[dict]` — Phase-4 coordinator primitive. Returns the ranked-but-unsorted movie delete-candidate dicts (same guards/tiers as `run_deletions`) without deleting. Reads the persisted `watchability_score` column, falling back to a live score map; if the column exists but is entirely empty it yields nothing (won't delete on fallback scores).
- `delete_selected_movie_files(instance, df, picks) -> dict` — Phase-4 coordinator primitive. DELETEs the picks the coordinator selected, records the restore-set, persists df. dry_run stamps the plan but issues no DELETE.
- `load_movie_files(instance)` — Coordinator helper: load the movie-files Parquet (or None).

Internal helpers (selection):
- `_resolve_instance`, `_get_movie_files_manager`, `_get_free_space_gb` (mount-deduped via `radarr_api.disk_free_gb`), `_space_targets` (returns `(T, U)` from `space_targets`, with the unconfigured-floor alert), `_coordinator_owns_deletion`, `_universe_delete_age_days`, `_fmt_bytes`.
- Ledger: `_ensure_plan_cols`, `_stamp_plan` (delegates the write to the brain `stamp`).
- Profiles: `_fetch_hd720p_profile`, `_profile_max_resolution` (static), `_fetch_ranked_profiles`.
- Scoring: `_build_active_collection_set`, `_build_affinity_inputs`, `_score_row`, `_build_score_map`, `_load_related_tmdb_ids`, `_row_critic_avg`.

- **Parent manager**: `RadarrQualityManager` (sets `parent_name = "RadarrQualityManager"`).
- **Submanagers loaded**: none.
- **External API endpoints**: `GET qualityprofile`, `GET movie/{id}`, `PUT movie/{id}`, `POST command` (`MoviesSearch`), `DELETE moviefile/{id}`, plus `radarr_api.disk_free_gb` / `disk_total_gb`.
- **config keys read**: `free_space_limit` (via `space_targets`), `space_pressure_delete_enabled` (default True), `space_pressure_include_unwatched` (default True), `space_pressure_score_ceiling` (default 20), `universe_delete_age_days` (default 0=off), `rating_groups` (members / grace_members), `scoring.related_graph` (`enabled` default True, `cap` default 4.0), `affinity_boost` (via `watch_likelihood.affinity_boost`), the space-coordinator ownership flag (via `coordinator_owns_deletion`), and `ignore`-style total-drive resolution.
- **global_cache keys**: reads `tautulli/affinity`, `trakt/history/movies`, `tautulli/group/{group}/tmdb_completions`, `radarr.movies.{instance}.full`, `tautulli/platforms`, `tautulli/transcode`, `tautulli/users/{user}/affinity`, `radarr.quality.{instance}` (profile list cache in upgrades). Writes the restore-set key `RadarrRepairAnomalyManager._DELETED_SET_KEY.format(inst=instance)` (fallback `radarr/{instance}/demote_deleted`).
- **Parquet keys**: reads/writes `watchability_score`, `watchability_breakdown`, `watchability_percentile`, `quality_profile_id`, `quality_profile_name`, `quality_action`, `marked_for_deletion`, `planned_action`, `plan_reason`, `plan_reclaim_gb`; reads `keep_policy`, `is_franchise_entry`, `movie_file_id`/`movie_id`, `tmdb_id`, `size_bytes`, `collection_name`, `last_watched_at`, `date_added`, critic columns.
- **dry_run**: strictly resolved in `__init__` (kwargs → parent → registry `RadarrManager` → `Main`; **raises `ValueError`** if none found). Every APPLY stage logs "Would …" and stamps the ledger but issues no Radarr writes; plan columns persist even in dry_run so the ledger previews the plan.
- **Related-graph file read**: `_load_related_tmdb_ids` reads `MOVIE_BUCKETS["related"]/{tmdb_id}.json.gz` (daemon-written) cache-only; missing/unreadable → empty set (C3 term degrades to 0).
- **Singleton / concurrency**: standard `BaseManager` singleton; no threading.

## How it functions

`__init__`: standard wiring + strict dry_run resolution (refuses to initialize without an explicit value).

`run(instance)` control flow:
1. Read free space (`_get_free_space_gb`) and the band `(T, U)` (`_space_targets`).
2. If `free >= U` → return `{"action": "none"}` (nothing to do).
3. Stage 0: `run_active_watcher_upgrades` (only acts if `free >= U`, so under genuine pressure this is a no-op).
4. Stage 1: `run_downgrades`.
5. Re-read free space.
6. Stage 2: `run_deletions` on the refreshed free space.
Returns before/after free space + per-stage stats.

Scoring path: `_build_score_map` does ONE `df.to_dict("records")` pass and scores each row via `_score_row`, which marshals the row into a `MovieFeatureRow` and calls the pure scorer in the brain. Inputs (genre affinity, watched-tmdb set, collection members, per-user/device/transcode context, related-graph neighbours) are pulled from `global_cache`. `refresh_scores` persists the scores + a per-signal-group breakdown JSON + a library percentile.

Brain delegation (modules named, not documented here):
- `machine_learning.features.movie_features` — `build_movie_feature_row`, `score_movie_features` (the row→features marshalling + the pure `score_movie`).
- `machine_learning.space.downgrade_planner.plan_movie_downgrades`, `machine_learning.space.upgrade_planner.plan_movie_upgrades`, `machine_learning.space.delete_planner` (`build_movie_delete_candidates`, `bare_universe_protected`).
- `machine_learning.scoring.critic.critic_avg`, `machine_learning.ledger.decision_ledger.stamp`.
- Support helpers: `watch_likelihood.affinity_boost`, `space_targets.space_targets` / `coordinator_owns_deletion`, `space_floor_alert.alert_unconfigured_floor`.

## Criteria & examples

- **Pressure band**: act only when `free < U`; deletion only when `free < T`. With `T = 100 GB`, `U = 110 GB`: at 130 GB free → only upgrades may run; at 105 GB free → upgrades skip, downgrades run (need `~U − free = 5 GB`); at 90 GB free → downgrades then deletions run, deleting until projected free reaches 110 GB.
- **Downgrade protection (`WATCHABILITY_PROTECT_THRESHOLD = 6`)**: a movie scoring ≥ 6 is protected from downgrade (household likely to re-watch). The brain also spares titles watched within `RECENT_WATCH_DAYS = 7`, keep_forever/keep_movie, and titles already at/below the 720p floor resolution.
- **Delete ceiling (`space_pressure_score_ceiling`, default 20)**: an unwatched movie is only delete-eligible if its score is `< 20`. Worked example: an unwatched movie scoring 18 (below the 20 ceiling), with `date_added` older than 30 days, becomes a tier-1 delete candidate; one scoring 25 is skipped.
- **Delete tiers & ordering**: tier 0 = watched + grace-expired (and `marked_for_deletion`); tier 1 = optional unwatched low-score. Within the ranked list, ordering is by `(tier, score, critic, -size)` — a missing critic sorts NEUTRAL at 5.0/10. Example: between two tier-0 movies scoring 10, the one with the lower critic average is deleted first; if critics tie, the larger file goes first.
- **Never-delete guards**: `keep_forever`, `keep_movie`, `keep_universe`, franchise entries / franchise file ids, anything watched within `COLLECTION_WINDOW_DAYS = 30`, and (when `universe_delete_age_days` is set) bare-`universe` titles still inside their on-disk ageing window (`bare_universe_protected`).
- **Active-collection downgrade nuance**: a movie in a collection where any member was watched in the last 30 days is kept but allowed to sit at 720p for now (it's "likely to be watched soon").
- **Empty-score safety (`build_delete_candidates`)**: if the persisted `watchability_score` column exists but is entirely NaN, the method yields NO candidates and logs a warning — refusing to rank every movie as deletable on fallback scores.
- **Restore-set**: every real deletion's tmdb_id is written to the restore-set keyed by instance, so `restore_recovered_deletions` (in the repair/anomaly manager) can re-acquire it if its score later recovers. A failure to persist the restore-set is logged LOUDLY as "NOT restorable".

## In plain English

Imagine your movie shelf is nearly full. This manager is the librarian who reacts in three escalating steps. First, if there's plenty of room, it *upgrades* the films your family is actually watching right now to the best quality (but never the kids' G/PG titles unless the adults watched them too). Second, when space gets tight, it *shrinks* the least-loved films one notch at a time — a 4K film becomes 1080p, then 720p — spreading the pain so no single film gets crushed, and it leaves the films you watch a lot or saw this week alone. Only as a true last resort does it *delete* anything, and even then only films you've already watched, that aren't part of a protected franchise, and that you haven't touched in a month — always starting with the lowest-rated. Crucially, it writes down everything it deletes (like a Princess Bride DVD), so if your tastes change and that film's score climbs back up, the system can re-download it later. In dry-run it just narrates "I would shrink/delete this" without touching anything.

## Interactions

- **Parent**: `RadarrQualityManager`.
- **Siblings**: `RadarrQualityUniverseManager` (tightly coupled — space-pressure downgrades a *bare* `universe` title as a last resort, and the two managers deliberately scope their decision-ledger stamps so they don't clobber each other; `refresh_scores` here feeds the watchability scores the universe manager uses to gate 4K), plus `RadarrFileSizesManager` (shared size model), `RadarrQualitySelectorManager`, `RadarrCustomFormatsManager`, `RadarrQualityAdjustmentManager`.
- **Other managers**: `RadarrCacheMovieFilesManager` (Parquet load/save + `_build_franchise_file_ids`), `RadarrRepairAnomalyManager` (provides the deleted-restore-set key), `TraktMoviesManager.people` (credits for scoring), the cross-service `SpaceCoordinatorManager` (calls `build_delete_candidates` / `delete_selected_movie_files` / `load_movie_files` for a unified movie+TV deletion pool), `RadarrManager` / `Main` (dry_run source).
- **Services**: `radarr_api`, `instance_manager`, `global_cache`; reads the daemon-written `movie_related` bucket on disk.
- **Brain modules** (named, not documented): `machine_learning.features.movie_features`, `machine_learning.space.{downgrade_planner,upgrade_planner,delete_planner}`, `machine_learning.scoring.critic`, `machine_learning.ledger.decision_ledger`.
