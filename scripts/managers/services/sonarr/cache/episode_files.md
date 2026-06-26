# SonarrCacheEpisodeFilesManager

- **File** — `scripts/managers/services/sonarr/cache/episode_files.py`
- **One-liner** — The Sonarr enrichment + lifecycle engine: a Parquet-backed, ML-ready cache of episode-file metadata for the two highest-signal subsets of the library (pilot files and watched files) that also drives the full episode lifecycle — pilot acquisition, next-episode prefetch, grace-period deletion, JIT quality upgrades/restores, per-series watchability scoring, and a queryable decision ledger.

This is by far the heaviest manager in the `sonarr/cache` package (~5,000 lines). It is the Sonarr twin of the Radarr space-pressure / scoring managers.

## What it does (for a senior Python engineer)

`SonarrCacheEpisodeFilesManager(BaseManager, ComponentManagerMixin)` is reachable as `sonarr_cache.episode_files`. It is loaded **manually** by `SonarrCacheManager` (excluded from the normal component map; see the package README for why).

### Storage & schema
- One Parquet per instance at `{key_builder.base_dir}/sonarr/{instance}/episode_files.parquet` (Snappy, pyarrow). Rows are sorted by `(series_id, season_number, episode_number)` on save for compression + predicate pushdown.
- `SCHEMA_COLUMNS` is a flat, ML-ready schema declared up front so missing API fields become NaN rather than KeyErrors. Notable columns: identity (`episode_file_id`, `series_id`, `season_number`, `episode_number`), signal flags (`is_pilot`, `is_watched`, `next_episode`, `watch_count`, `last_watched_at`, `all_household_watched`, `household_last_watched_at`, `percent_complete`), lifecycle (`marked_for_deletion`, `available_until`, `keep_policy`), file/quality/video/audio media-info columns, a **decision ledger** (`planned_action` ∈ {delete, upgrade, acquire, downgrade, None}, `plan_reason`, `plan_reclaim_gb`), and **watchability** (`watchability_score`, `watchability_percentile`, `watchability_breakdown`). Several runtime columns are added on demand (`pilot_search_attempts`, `pilot_last_searched_at`, `pilot_last_profile_id`, `pilot_successful_profile_id`, `pre_upgrade_quality`, `upgraded_for_watching`).

### Two tracked subsets
- **Pilot files** — the representative earliest episode file per series (codec/quality fingerprint), filled incrementally in background batches.
- **Watched files** — every episode found in Tautulli history, enriched with `watch_count` / `last_watched_at` / `percent_complete` and household-watch state. The docstring calls this "the strongest ML signal in the system: what quality did the user actually choose to consume?"

### Key public methods
- `load(instance)` / `save(instance, df)` — read/write the Parquet, casting `_NUMERIC_COLUMNS` to float64 to keep dtypes stable across partial runs.
- `refresh_scores(instance)` — compute a **per-series** watchability score and broadcast it onto every episode row of that series (`watchability_score`), plus a library-wide `watchability_percentile` (ranked over distinct series so long shows don't dominate) and a JSON `watchability_breakdown`. Persisted even in `dry_run` (non-destructive annotation that downstream sorts on, "least valuable first"). Delegates the actual scoring to the brain (see below).
- `run_pilot_batch(instance, all_series, batch_size=PILOT_BATCH_SIZE)` — fetch pilot-file metadata for series not yet in the Parquet. Watched series first (uncapped), then up to `batch_size` unwatched. Classifies existing rows into real pilots / fresh stubs / stale stubs, upgrades stale stubs to real rows when a file finally appears, and writes the Parquet (even in dry_run — read-only mirror).
- `run_pilot_search(instance)` — for stub pilots (no file), trigger `EpisodeSearch` for S01E01; on repeated empty results step the quality profile UP one tier (widen the net); throttle re-searches to `PILOT_SEARCH_INTERVAL_H` (24 h) with optional exponential backoff. APPLY (search PUT/POST), dry-run-gated.
- `sync_from_tautulli(instance)` — the master lifecycle entry point (see "How it functions").
- `run_jit_quality_upgrades(instance)` — just-in-time bump of the next-up unwatched episodes of a series to the best quality profile that still leaves the reserve free; fires `EpisodeSearch`; a background worker reverts the series profile afterward. Skips kids-cert and keep-tagged series, and bails if free space ≤ reserve. APPLY, dry-run-gated.
- `run_jit_quality_restores(instance)` — once a JIT-upgraded episode is watched ≥ 80%, PUT its file quality back to the `pre_upgrade_quality` snapshot and clear the JIT flags.
- `build_delete_candidates(instance, df=None)` / `delete_selected_episode_files(instance, file_ids)` / `restore_recovered_episode_deletions(instance)` — the cross-service **SpaceCoordinator** hooks (see below).
- Standalone maintenance wrappers: `purge_sonarr_deleted(instance)`, `cleanup_non_essential(instance)`, `delete_marked_files(instance)`.
- `get_summary(instance)` — diagnostic stats (row/pilot/watched counts, total size GB, codec/resolution distributions, HDR count).

### FETCH / CACHE / APPLY
All three, and this is the one manager in the package that genuinely **APPLIES** decisions to Sonarr:
- **FETCH** — episode lists, episode files, quality profiles, queue, root-folder free space, all via `self.sonarr_api._make_request(instance, endpoint, ...)` and helpers like `disk_free_gb`.
- **CACHE** — the per-instance Parquet; `sonarr/{instance}/episodes/by_series/{series_id}` (24 h TTL episode lists); `sonarr/{instance}/episodes/pilots`; `sonarr/{instance}/deleted_episodes` (restore tracking).
- **APPLY** — Sonarr writes: `episodefile/{id}` DELETE, `episode/monitor` PUT, `command` POST (`EpisodeSearch`/`SeriesSearch`), per-series quality-profile PUTs, queue cancels. Every APPLY path checks `self.dry_run` and logs a "would …" line instead.

### External API endpoints touched
`episode?seriesId={id}`, `episodefile` (+ `episodefile/{id}` DELETE), `episode/monitor` (PUT), `command` (POST), `qualityprofile`, `series/{id}` / `series` (PUT for profile changes), the queue endpoint, and root-folder/disk-space endpoints — all through `sonarr_api._make_request`.

### Config keys read
`scoring.show_user_rating`, `scoring.related_graph`, `rating_groups` (members / grace_members), `trakt.username`, `owned_restore_score_threshold` (default 20), `acquisition.next_episode` (`graduated_cap`, `recency_gate.cold_days`, `budget_ramp`), `household_watch_quorum` (enabled/fraction), `grace_window_ramp`, `pilot_backoff`, `jit_space_band` (enabled/headroom_gb/cap_resolution), plus `free_space_limit`-derived targets via the shared `space_targets` / `coordinator_owns_deletion` utilities and `dry_run`.

### Class constants / thresholds
`CACHE_MAX_AGE=172_800` (48 h stub re-check), `GRACE_HOURS=3`, `RECENT_AIR_DAYS=30`, `PREFETCH_HOURS=3.0`, `MIN_FREE_SPACE_GB=50.0` (last-resort floor only), `JIT_MAX_EPISODES=3`, `JIT_RESERVE_PCT=0.05`, `EPISODES_CACHE_TTL_S=86_400`, `_RESTORE_TRACK_MAX_AGE_S=30 days`, `PILOT_BATCH_SIZE=None` (unlimited).

### Singleton / concurrency / threading
- `BaseManager` singleton.
- `dry_run` is resolved by walking kwargs → parent manager → `SonarrManager` → `Main`, and **raises** if unresolvable rather than silently defaulting to live mode (this manager is destructive, so it refuses to guess).
- `sonarr_api` is resolved defensively (kwarg → `manager.sonarr_api` → registry `SonarrManager`), rejecting any object lacking `_make_request`.
- **Threading:** JIT upgrades spawn a background worker (`_spawn_jit_search_worker` → `_jit_search_worker`) that waits for each `EpisodeSearch` to finish and then reverts the series quality profile. The worker cannot safely write the Parquet concurrently with the main pipeline, so it records failures to a side cache that `_reconcile_failed_jit` consumes on the next run.

## How it functions

Lifecycle: constructed by `SonarrCacheManager` → `__init__` resolves `sonarr_api` / `instance_manager` / `dry_run` defensively and registers. There is no `load_components` (no submanagers); the "components" here are the brain modules it imports.

`sync_from_tautulli(instance)` is the orchestrator and runs this pipeline in order:
1. `_collect_tautulli_episode_history()` → `{(title, season, episode): watch}`.
2. Match each title to a Sonarr series via `sonarr_cache.series.get_series_by_title`; update existing rows or resolve+add new rows (`_resolve_episode_file` + `_normalise`), stamping household-watch state (`_resolve_household_watch_state`, optional quorum).
3. `_sync_keep_policies` — stamp `keep_policy` from Sonarr tags (must precede grace).
4. `_compute_next_episodes` — flag `next_episode=True` across a runtime budget (`PREFETCH_HOURS`), hottest-series-first.
5. `_do_acquire_next_episodes` — monitor + `EpisodeSearch` the flagged-but-fileless rows (gated above the band top U).
6. `_apply_grace_period` — set `available_until = last_watched_at + GRACE_HOURS` and `marked_for_deletion` once expired, honouring pilot / keep / recent-air / household guards.
7. Deletion: if `coordinator_owns_deletion(config)`, keep the marks but defer the actual delete to the cross-service coordinator; otherwise `_do_delete_marked_files` + `_do_purge_sonarr_deleted`.
8. `_do_cleanup_non_essential`.
9. Stamp the decision ledger (`planned_action`/`plan_reason`/`plan_reclaim_gb`) for acquire/delete with multi-episode reclaim de-duping, then `save` (even in dry_run).

Whole-file delete safety: `_build_protected_file_ids` collapses every guard down to the set of `episode_file_id`s touched, so if **any** row sharing a file id is guarded (multi-episode omnibus file), the whole file is protected — preventing a guarded sibling from being destroyed (the project's "whole-file delete guards" invariant). On any failure building this set, the delete paths **fail safe** and delete nothing.

### Decisions delegated to `machine_learning` (named, not documented here)
- `classification.guards` — `build_pilot_file_ids`, `build_protected_file_ids` (the delete-guard predicates).
- `features.show_features` — `build_show_feature_row`, `score_show_features` (per-series watchability scoring).
- `acquisition.next_episode_planner` — `last_watched_per_series`, `build_runtime_lookup`, `episode_cap`, `order_series_by_recency`, `series_budget_multiplier`, `is_cold_series`, and the `DEFAULT_*` cap/gate/ramp policies (next-episode prefetch).
- `acquisition.pilot_stepping` — `rank_pilot_profiles`, `profile_max_resolution`, `pilot_search_due`, `next_pilot_profile`, `pilot_backoff_interval` (pilot search + profile stepping).
- `lifecycle.grace_policy` — `episode_grace_decision`, `grace_mark`, `grace_window_multiplier`.
- `lifecycle.household_watch` — `resolve_household_watch`.
- `space.jit_planner` — `jit_candidates`, `choose_jit_profile`, `jit_reserve_gb`, `jit_row_skip`, `jit_step_down_pids`.
- `sizing.size_model` — `estimate_gb_for_profile`, `measured_mb_per_min`, `profile_max_quality`, `CALIBRATED_MB_PER_MIN` (size estimates).
- Plus the support utilities `watch_likelihood` / `resolution_cap_for_likelihood`, `space_targets` / `coordinator_owns_deletion`, `alert_unconfigured_floor`.

## Criteria & examples

- **Grace period.** A watched, non-pilot, non-next-episode episode with `last_watched_at = 2026-06-10T08:00Z` gets `available_until = 11:00Z` (`GRACE_HOURS=3`); once "now" passes 11:00Z it is marked for deletion — unless a guard fires.
- **Recent-air guard.** An episode that aired 12 days ago (< `RECENT_AIR_DAYS=30`) is never deleted even if watched and grace-expired.
- **Household guard.** With a household of Alice + Bob, an episode Alice watched but Bob hasn't has `all_household_watched=False` → its file id is protected (grace countdown doesn't apply). With `household_watch_quorum.fraction=0.5` of 2 members, the ceil is 1 → one watcher is enough to count it household-watched.
- **Pilot stub upgrade.** A series whose pilot row is a stub (`is_pilot=True`, `episode_file_id=None`) older than `CACHE_MAX_AGE` (48 h) is re-checked next run; if Sonarr now has a file the stub is upgraded to a real pilot row (and the successful profile id is carried over so JIT restore never downgrades below it).
- **Restore.** A coordinator-deleted series whose `watchability_score` later recovers above `owned_restore_score_threshold` (default 20) — e.g. score climbs to 35 > 20 — is re-monitored and `EpisodeSearch`-ed. A recovered series is held (`cooling`, no API call) until `owned_restore_min_age_days` (default 0 = off) have elapsed since its deletion `ts`, mirroring Radarr's re-grab cooldown so a score hovering at the floor can't thrash delete↔restore. Unresolved coords are deferred (retried) up to 30 days before being dropped.
- **JIT upgrade.** With 5% reserve on a 1,000 GB drive, if free space is 30 GB and the reserve is 60 GB, JIT upgrades are skipped entirely. Otherwise each next-up episode is bumped to the highest profile whose estimated grab keeps free above the reserve, capped at `JIT_MAX_EPISODES=3` per series. `run_jit_quality_restores` only reverts after the episode is ≥ 80% watched.
- **Delete-candidate safety.** If `watchability_score` was never populated this run (column empty), `build_delete_candidates` yields **nothing** — it refuses to rank every marked episode as maximally deletable on fallback scores.

## In plain English

Think of this as the smart shelf manager for your TV box sets. It keeps a tidy spreadsheet of two things it cares about most: the very first episode of every show (so it knows what quality each show is), and every episode anyone has actually watched (so it knows what you really like and in what quality you chose to watch it).

When you finish an episode of, say, The Mandalorian, it waits a short grace period and then frees up the space — but it will never throw out a pilot, anything you tagged "keep" (like every episode of Bluey), anything that just aired, or anything the whole household hasn't finished yet. It also peeks ahead: if you're mid-season, it quietly pre-downloads the next few episodes so they're ready, and can briefly fetch a sharper copy of the one you're about to watch, then swap it back to a smaller copy once you're done — like upgrading to the IMAX print just for tonight, then returning it. And if it ever deletes something that later turns out to be popular again, it remembers and can re-grab it. Crucially, in "dry run" mode it writes down exactly what it *would* do (and how much space it would free) without touching anything, so you can review the plan first.

## Interactions

- **Parent manager:** `SonarrCacheManager` (attached as `episode_files`, manually loaded). Ultimately under `SonarrManager` → `Main`.
- **Sibling submanagers:** `sonarr_cache.series` (title/id lookups, library iteration), and the broader `sonarr_cache` proxy for shared cache access.
- **Other services:** the `sonarr_api` gateway (`SonarrInstanceManager`) for all Sonarr FETCH/APPLY; Tautulli history (via `_collect_tautulli_episode_history`); `TraktShowCacheManager` (lazily built in `_get_show_cache` for cache-only credits/ratings/related-shows used in scoring); the cross-service **SpaceCoordinator** (consumes `build_delete_candidates`, calls `delete_selected_episode_files`, and the manager mirrors via `restore_recovered_episode_deletions`); `GlobalCacheManager`, `RegistryManager`.
- **Brain modules:** the eight `machine_learning` packages listed above (guards, show features, next-episode planner, pilot stepping, grace policy, household watch, jit planner, size model) — this manager FETCHes/CACHEs/APPLIEs; those modules make the value judgements.
