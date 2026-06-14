# SonarrSeriesQualityManager

- **File** — `scripts/managers/services/sonarr/series/quality.py`
- **One-liner** — Manages Sonarr series quality profiles: assigns/refreshes default profiles and, when free space is comfortable, upgrades the quality profile of shows the household is actively watching.

## What it does (for a senior Python engineer)

`SonarrSeriesQualityManager(BaseManager, ComponentManagerMixin)` is a child of `SonarrSeriesManager`. It loads no submanagers of its own (`prepare()` notes "no subcomponents declared"). It performs FETCH (Sonarr `qualityProfile` lists, `GET series/{id}`, `tag` lists), reads CACHE (episode_files Parquet, `sonarr.tags.{instance}`, profile cache), and APPLY (`PUT series/{id}` to change the profile). Its headline operation is `run_active_watcher_upgrades`; the rest are profile CRUD utilities.

Key public methods:
- `run()` — a near no-op (logs a banner). The real upgrade pass is invoked elsewhere via `run_active_watcher_upgrades`, not by the parent's component `run()` loop.
- `run_active_watcher_upgrades(instance) -> dict` — the core. Returns a stats dict (`checked`, `upgraded`, `already_best`, `skipped_kids`, `skipped_not_active`, `skipped_keep`, `skipped_fully_downloaded`, `skipped_quality_frozen`, `failed`). Upgrades actively-watched, non-kids, non-frozen, not-fully-downloaded series to the highest-resolution profile available, but only when free space sits above the upgrade band top `U`.
- `get_free_space_gb(instance) -> float` — delegates to `sonarr_api.disk_free_gb(instance)` (mount-deduped free GiB).
- `get_series_profile_id(instance, series_id) -> int | None` — current `qualityProfileId` for a series (via retrieval fetch).
- `update_series_profile(instance, series_id, profile_id) -> bool` — fetches the series record, sets `qualityProfileId`, and `PUT series/{series_id}`.
- `assign_default_profile_if_missing(series_data, instance) -> dict` — stamps the default profile id onto a series dict if it lacks one.
- `get_default_quality_profile(instance) -> int` — first profile id from cache (`sonarr_cache.quality.get_profiles`) or the `qualityProfile` API; falls back to `1`.
- `_get_series_data(instance, series_id) -> dict | None` — thin wrapper over `manager.retrieval.fetch.get_series_by_id`.
- `batch_update_profiles(instance, updates: list[tuple[int,int]]) -> dict` — applies a list of `(series_id, profile_id)` updates, returns a per-id ✅/❌ map.
- `bulk_assign_defaults_if_missing(instance, series_list) -> list[dict]` — assigns defaults across a list, returns the mutated list.
- `refresh_all_series_profiles(instance, use_default_if_missing=True)` — walks every series from `sonarr_api.get_series` and re-PUTs each profile (optionally defaulting missing ones); returns nothing but logs an `{updated, skipped, defaulted}` summary.

Class constants:
- `ACTIVE_WATCH_DAYS = 30` — watched within this window = "active".
- `KIDS_CERTS = {"g","pg","tv-g","tv-y","tv-y7"}` — kids-only certs skip upgrading.
- `FREEZE_QUALITY_TAGS = {"keep_quality","keep-quality","keepquality"}` — frozen series are never upgraded.

External API endpoints touched: `GET/PUT qualityprofile` and `qualityProfile`, `GET/PUT series/{id}`, `GET series`, `GET tag`. global_cache read: `sonarr.tags.{instance}`. Parquet read: episode_files (via `SonarrCacheEpisodeFilesManager.load(instance)` from the registry). No Parquet writes here.

dry_run: in `run_active_watcher_upgrades`, when `self.dry_run` is true each qualifying series logs `[dry_run] Would upgrade ...` and increments `upgraded` without issuing the PUT. The summary line is prefixed `[dry_run]`. dry_run is resolved from kwargs or the parent (`getattr(manager, "dry_run", False)`).

Singleton/threading: standard `BaseManager` singleton; per-series evaluation is a sequential loop.

## How it functions

Init wires the dual cache, `orchestration`, `instance_manager`, `sonarr_api`, and `dry_run` from kwargs/parent, then `register()`.

`run_active_watcher_upgrades` control flow:
1. Resolve the instance via `instance_manager.resolve_instance`.
2. **Space gate** — read `free_gb` and `disk_total_gb`, call `alert_unconfigured_floor(...)`, then `space_targets(config, total_gb=...)` to get `(_, upgrade_floor)` = the band top `U` (`free_space_limit` + headroom, or 25% of total drive when unset). If `free_gb < upgrade_floor`, log and return early — no upgrades under pressure.
3. Load episode_files Parquet via the registry `SonarrCacheEpisodeFilesManager`; bail on empty or if `last_watched_at`/`series_id` columns are absent.
4. **Aggregate per-series signals** via the brain `space.upgrade_planner.aggregate_series_signals(df)`.
5. Fetch quality profiles, rank ascending by `_max_res(...)` (the deepest allowed resolution across `items`/nested `items`), and pick the top profile as the upgrade target.
6. Resolve the target MiB/min once (measured per-quality average → `JIT_FALLBACK_MB_PER_MIN` → `25.0`) using the episode_files manager's `_profile_max_quality`, `_measured_mb_per_min`, and `JIT_FALLBACK_MB_PER_MIN` — used for the anticipated-space "GB to grab" detail.
7. **df-based candidate filter** via the brain `space.upgrade_planner.active_series_candidates(series_data, cutoff, kids_certs)`, which returns the active list plus the `checked / skipped_keep / skipped_not_active / skipped_kids` tallies (run before any API call so skipped series cost nothing).
8. For each active series: fetch the live series record; apply `series_fully_downloaded(series)` (brain) record-only guard FIRST; resolve tag labels (cache `sonarr.tags.{instance}` → `tag` API); then call the brain `decide_series_upgrade(series, tag_labels, best_id, freeze_tags, mbpm)` for the quality-freeze / already-best verdict and the upgrade numbers (`ep_total`, `ep_file_count`, `remaining`, `est_gb`).
9. Build a human-readable "why" detail (recency, watched/household eps, GB to grab) and either log a dry-run line or `PUT series/{sid}` with the new `qualityProfileId`, tallying `upgraded` / `failed`.

Brain modules delegated to (documented elsewhere): `machine_learning.space.upgrade_planner` — `active_series_candidates`, `aggregate_series_signals`, `decide_series_upgrade`, `series_fully_downloaded`.

## Criteria & examples

- **Space gate**: with `free_space_limit` unset on a 1000 GB drive, `U` defaults to ~250 GB. If only 180 GB is free (180 < 250), the entire pass is skipped.
- **Active window**: `ACTIVE_WATCH_DAYS = 30`. A show last watched 12 days ago is active; one last watched 45 days ago is `skipped_not_active`.
- **Kids-only**: a series whose only certifications are in `{g, pg, tv-g, tv-y, tv-y7}` is `skipped_kids` unless adults also watch it (the brain's candidate filter decides).
- **Freeze tag**: a series tagged `keep_quality` returns verdict `skip == "quality_frozen"` and is `skipped_quality_frozen`.
- **Already best**: a series already on the top-ranked profile returns `skip == "already_best"`.
- **Fully downloaded**: a show with `episodeFileCount == episodeCount` (e.g. 24/24) is `skipped_fully_downloaded` because the JIT upgrade path, not this active-watcher pass, handles already-complete shows.
- **Upgrade detail example**: a show on `HD-1080p (≤1080p)` with 20/24 episodes on disk, last watched 8 days ago, would log `HD-1080p (≤1080p) → Ultra-HD (≤2160p) | why: actively watched — watched 8d ago (≤30d active window) · 3 ep watched · 20/24 ep on disk (~14.2 GB to grab 4 remaining at ≤2160p)`.

## In plain English

Imagine you have a streaming library and you only want to spend extra disk space upgrading shows you actually watch. This manager waits until the drive has comfortable breathing room, then looks at which shows someone in the house watched in the last month. For those — as long as they aren't kids-only cartoons, aren't tagged "keep this quality as-is," and aren't already in the best picture quality you have — it bumps them up to the sharpest profile (say from 1080p to 4K) and lets Sonarr grab the better files. If the drive is tight, it does nothing, so it never starves space just to make a show prettier.

## Interactions

- **Parent manager**: `SonarrSeriesManager`.
- **Sibling submanagers**: reaches `manager.retrieval.fetch` for series records; coordinates with `SonarrSpacePressureManager` indirectly (upgrades only run when space is healthy; downgrades run when it isn't).
- **Brain modules**: `machine_learning.space.upgrade_planner` (candidate selection + per-series upgrade verdicts).
- **Other services / registry**: Sonarr HTTP API (`sonarr_api`), `instance_manager`, the registry-resolved `SonarrCacheEpisodeFilesManager` (episode_files Parquet + size helpers), and shared utilities `space_targets` / `alert_unconfigured_floor`.
