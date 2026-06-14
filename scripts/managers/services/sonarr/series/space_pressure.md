# SonarrSpacePressureManager

- **File** — `scripts/managers/services/sonarr/series/space_pressure.py`
- **One-liner** — Stage-1 TV space relief: when the drive is in the pressure band, it steps the lowest-watchability series DOWN the resolution ladder (toward HD-720p) and re-grabs them at the lower quality, freeing space without deleting anything.

## What it does (for a senior Python engineer)

`SonarrSpacePressureManager(BaseManager, ComponentManagerMixin)` is the Sonarr twin of `RadarrSpacePressureManager.run_downgrades` and a child of `SonarrSeriesManager`. It loads no submanagers. It performs FETCH (Sonarr `qualityProfile` lists, `GET series/{id}`), CACHE (reads + writes the episode_files Parquet plan columns), and APPLY (`PUT series/{id}` to lower the profile, then `POST command` `SeriesSearch` to re-grab). It is non-destructive and reversible — the show stays, only at a lower quality.

Key public methods:
- `run_downgrades(instance, free_space_gb) -> dict` — the core. Downgrades the lowest-watchability series until projected free space reaches `U`. Returns a stats dict (`candidates`, `downgraded`, `est_reclaim_gb`, `skipped_protected`, `skipped_high_score`, `skipped_recent`, `skipped_already`, `failed`). `free_space_gb` is supplied by the orchestration, which has already confirmed `free < U`.
- `run() -> {}` — deliberate no-op; the downgrade pass is driven by the orchestration wrapper (`run_space_pressure_downgrades`) AFTER `refresh_scores`, so it operates on fresh per-series watchability scores. `prepare()` is also a no-op.
- `get_free_space_gb(instance) -> float` — `sonarr_api.disk_free_gb(instance)` (returns `inf` if no api).

Notable helpers: `_space_targets(instance)` returns `(T, U)` from `space_targets(...)` (floor `T`, band top `U`); `_score_ceiling()` reads the config ceiling; `_fetch_hd720p_profile` / `_fetch_ranked_profiles` pull and rank Sonarr profiles by max resolution; `_profile_max_resolution` delegates to the brain `sizing.size_model.profile_max_quality`; `_ensure_plan_cols` / `_stamp_plan` manage the Parquet plan ledger (the latter delegates to the brain `ledger.decision_ledger.stamp`).

Class constants:
- `HD_720P_PROFILE_NAME = "HD-720p"` — the floor profile name (matched case-insensitively).
- `PRESSURE_FALLBACK_GB = 25.0` — last-resort floor only (used when `free_space_limit` AND total drive are both unreadable).
- `RECENT_WATCH_DAYS = 7` — don't downgrade a series watched within 7 days.
- `RECENT_AIR_DAYS = 30` — don't downgrade a series with an episode aired within 30 days (no Radarr analog).
- `DEFAULT_SCORE_CEILING = 20` — the `tv_space_pressure_score_ceiling` default (0–100 scale).
- `DEFAULT_RUNTIME_MIN = 45.0` — fallback per-episode runtime when unknown.
- `KEEP_TAGS = {keep_series, keep_season, keep_universe, keep_forever}` — protected; never downgraded.

Config keys read: `tv_space_pressure_score_ceiling` (default 20); `free_space_limit` (consumed indirectly via `space_targets`). global_cache: none directly. Parquet read/write: episode_files via the registry-resolved `SonarrCacheEpisodeFilesManager` — reads `series_id` / `watchability_score`, ensures + writes the plan columns `planned_action`, `plan_reason`, `plan_reclaim_gb`, then `ef.save(instance, df)`.

External API endpoints touched: `GET qualityProfile`, `GET series/{id}`, `PUT series/{id}`, `POST command` (`{"name":"SeriesSearch","seriesId":sid}`).

dry_run: resolved robustly in `__init__` — kwargs → parent → registry `SonarrManager` → registry `Main`; if still unresolvable it RAISES `ValueError` rather than silently defaulting to live writes (this is the documented dry_run-propagation footgun guard). When dry_run is true, the plan is STILL stamped and the Parquet STILL saved (so the decision is previewable), but no `PUT`/`SeriesSearch` fires and each candidate logs `[dry_run] Would step down ...`.

Singleton/threading: standard `BaseManager` singleton; per-candidate apply is a sequential loop, with the re-grab `SeriesSearch` commands issued in a second sequential pass.

## How it functions

Lifecycle: `__init__` (wire caches/api/instance_manager, resolve dry_run with the raise-if-unknown guard, `register()`) → the orchestration calls `run_downgrades(instance, free_space_gb)` after scores are refreshed.

`run_downgrades` control flow:
1. Resolve the episode_files manager from the registry; bail if missing, empty, or missing `series_id` / `watchability_score`.
2. Fetch and rank quality profiles ascending by resolution; resolve the HD-720p floor profile (default `floor_resolution = 720` if not found).
3. `_ensure_plan_cols(df)` and clear any stale `planned_action == "downgrade"` rows from a prior run (leaving delete/acquire/upgrade plans intact).
4. Compute `(_, U)` from `_space_targets`, `need_gb = max(0, U - free_space_gb)`, the score `ceiling`, and the `watch_cutoff` (7d) / `air_cutoff` (30d) timestamps.
5. **DECISION** — call the brain `space.downgrade_planner.plan_series_downgrades(df, ranked_profiles, need_gb, ceiling, watch_cutoff, air_cutoff, keep_tags, default_runtime_min, floor_resolution)`. The brain steps the lowest-watchability series DOWN one resolution tier at a time, spread across the pool, until ~`need_gb` is reclaimed (no single show is crushed straight to 720p). It returns `(candidates, _pstats)`; the stats (including `target_met`) are merged in.
6. **APPLY** — for each candidate: stamp the downgrade on ONE representative episode row (`c["indices"][0]`) with the WHOLE-series reclaim (stamping every row would inflate both the plan-ledger row count and GB total); then, unless dry_run, `GET series/{sid}`, set `qualityProfileId = c["target_id"]`, `PUT series/{sid}`, and queue the sid for re-grab.
7. Issue one `POST command SeriesSearch` per downgraded series to re-grab at the lower profile.
8. If anything changed, `ef.save(instance, df)`. Log a summary (dry-run-prefixed when applicable) with reclaim, target met/not-met, and all skip counts.

Brain modules delegated to (documented elsewhere): `machine_learning.space.downgrade_planner.plan_series_downgrades` (candidate selection + step-down ladder), `machine_learning.sizing.size_model.profile_max_quality` (resolution extraction), `machine_learning.ledger.decision_ledger.stamp` (plan-ledger writes).

## Criteria & examples

- **Pressure band**: only runs when `free < U`; the orchestration verifies this before calling. With `free_space_limit` unset on a 4000 GB drive, the floor `T` defaults to 1000 GB and `U` ≈ 1100 GB; the manager downgrades until projected free reaches `U`.
- **Score ceiling** (`tv_space_pressure_score_ceiling`, default 20): a series with `watchability_score = 14` (< 20) is eligible; a series scoring `27` (≥ 20) is `skipped_high_score`.
- **Recent-watch guard** (`RECENT_WATCH_DAYS = 7`): a low-scoring show watched 3 days ago is `skipped_recent`; one last watched 40 days ago is eligible.
- **Recently-aired guard** (`RECENT_AIR_DAYS = 30`): a low-scoring show whose newest episode aired 10 days ago is `skipped_recent` (don't downgrade something currently airing).
- **Keep tags**: a series tagged `keep_series` is `skipped_protected`, regardless of score.
- **At/below floor** (`floor_resolution`, e.g. 720): a show already on HD-720p is `skipped_already`.
- **Reclaim example**: a 1080p show with 24 episodes at ~1.5 GB each (~36 GB) stepped down to 720p might reclaim ~18 GB; the brain keeps adding such candidates (lowest score first) until the running `reclaimed` approaches `need_gb`, then stops, so a big library doesn't trigger a re-grab storm.

## In plain English

When your drive is getting full, instead of deleting shows, this is the "shrink, don't toss" step. It finds the shows nobody really watches anymore — and that aren't currently airing or recently seen, and aren't marked "keep" — and quietly knocks them down a notch in picture quality (4K to 1080p, or 1080p to 720p), re-downloading the smaller files. You still have every episode of, say, that old reality show you stopped following; it just takes up less room now. And it only shrinks as many shows as it needs to in order to get back to a healthy amount of free space, picking the least-watched ones first.

## Interactions

- **Parent manager**: `SonarrSeriesManager`. Driven by the Sonarr space orchestration (`run_space_pressure_downgrades`) after `refresh_scores`, not by the parent's component `run()` loop.
- **Sibling submanagers**: the inverse of `SonarrSeriesQualityManager` (which UPGRADES when space is healthy); both read the same episode_files Parquet / watchability scores.
- **Brain modules**: `machine_learning.space.downgrade_planner`, `machine_learning.sizing.size_model`, `machine_learning.ledger.decision_ledger`.
- **Other services / registry**: Sonarr HTTP API, `instance_manager`, the registry-resolved `SonarrCacheEpisodeFilesManager` (episode_files Parquet), `SonarrManager` / `Main` (for dry_run resolution), and shared utilities `space_targets` / `alert_unconfigured_floor`.
