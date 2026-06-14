# RadarrQualityUniverseManager

**File** — `scripts/managers/services/radarr/quality/universe.py`
**One-liner** — Manages quality (not deletion) for cinematic-universe / franchise movies: when disk is tight it downgrades their quality profile, when disk is plentiful it upgrades them back, and it never deletes them.

## What it does (for a senior Python engineer)

`RadarrQualityUniverseManager(BaseManager, ComponentManagerMixin)` is the quality-change manager for "universe"-tagged movies (MCU, DC, etc.). The contract: **universe titles are never deleted** — quality is the only lever, so under space pressure they are downgraded and under surplus they are upgraded back. It is FETCH (GET qualityprofile / movie / tag / movie list), APPLY (PUT movie with new `qualityProfileId`), and it reads/writes the movie-files Parquet (not `global_cache`) via `RadarrCacheMovieFilesManager`.

Tag conventions (Radarr hyphenated): `keep-universe` → label `universe`; `keep-universe-mcu` → `mcu`; `keep-universe-dc` → `dc`; multiple tags captured. In the Parquet these become `keep_policy ∈ {"keep_universe", "universe"}` with a `universe_name` string (pipe-joined for multiple labels).

Class attributes: `SCORE_4K_THRESHOLD = 70`, `_4K_MIN_RESOLUTION = 2000`.

Public methods:
- `audit_universe_tags(instance) -> dict` — Diagnostic. Compares universe tags/movies in live Radarr against `keep_policy=='universe'` rows in the Parquet; logs mismatches and guidance. Called automatically by `run` when `universe_count == 0`.
- `get_universe_movies(instance) -> pd.DataFrame` — Returns the Parquet rows where `keep_policy ∈ {keep_universe, universe}`.
- `get_universe_summary(instance) -> dict` — Groups universe movies by `universe_name` (splitting on `|`) into `{label: [{title, year, quality_profile_name, quality_action, size_gb}]}`.
- `evaluate_quality_actions(instance, free_space_gb, downgrade_threshold_gb=None, upgrade_threshold_gb=None) -> dict` — DECISION + persist. Writes `quality_action ∈ {"downgrade","upgrade",None}` into the Parquet for all universe rows based on free space vs the shared band; does NOT call the Radarr API. Returns a stats dict. **Persists even in dry_run** (so the downstream apply pass and decision ledger see the marks).
- `apply_quality_actions(instance, min_rank=0) -> dict` — APPLY. For each pending universe row: resolve the target profile (likelihood-gated upgrade ladder, or one-tier downgrade), GET the full movie payload, set `qualityProfileId`, PUT it back, clear `quality_action`, and stamp the decision ledger. Honors dry_run (logs "Would …", stamps the plan only, writes no profile). Returns stats.
- `run(instance, free_space_gb, downgrade_threshold_gb=None, upgrade_threshold_gb=None, min_rank=0) -> dict` — Convenience: `evaluate_quality_actions` → (audit if no universe movies) → `apply_quality_actions`; returns merged stats.

Internal helpers:
- `_resolve_instance`, `_get_movie_files_manager` (registry lookup of `RadarrCacheMovieFilesManager`).
- `_fetch_ranked_profiles(instance)` — GETs `qualityprofile`, sorts ascending by max allowed resolution (including nested group items).
- `_get_adjacent_profile` (legacy single-step, kept for compatibility), `_profile_max_resolution` (static), `_get_target_profile` (delegates up/down to the brain), `_downgrade_target` (delegates to the brain), `_stamp_universe_plan` (delegates to the ledger brain).

- **Parent manager**: `RadarrQualityManager`.
- **Submanagers loaded**: none.
- **External API endpoints**: `GET qualityprofile`, `GET tag`, `GET movie`, `GET movie/{id}`, `PUT movie/{id}`.
- **config keys read**: `free_space_limit` and total-drive size are consumed indirectly through `space_targets` / `disk_total_gb`; `self.config` is also passed into the brain helpers (e.g. watch-likelihood/upgrade-target config).
- **global_cache keys**: reads `radarr.movies.{instance}.full` (used in the audit as a movie source).
- **Parquet keys**: reads/writes the movie-files Parquet columns `keep_policy`, `universe_name`, `quality_action`, `quality_profile_id`, `quality_profile_name`, plus ledger columns `planned_action`, `plan_reason`, `plan_reclaim_gb` (via the brain stamp).
- **dry_run**: strictly resolved — `__init__` walks kwargs → parent → registry `RadarrManager` → registry `Main`, and **raises `ValueError` if it cannot find an explicit value** (refuses to default to False to avoid accidental destructive ops). Applies dry-run gating in `apply_quality_actions`.
- **Singleton / concurrency**: standard `BaseManager` singleton; no threading.

## How it functions

Lifecycle: `__init__` (wiring + strict dry_run resolution) → methods invoked by `run`. There is no `load_components`.

`run` flow:
1. `evaluate_quality_actions` computes the band `(T, U)` via `space_targets(self.config, total_gb=disk_total_gb(instance))` (T = `free_space_limit` floor or 25% of total when unset; U = band top). It then asks the brain `universe_action(free_space_gb, T, U)` for the desired action — downgrade below T, upgrade above U, hold (clear) in `[T, U]` (hysteresis, DEFECT-2 fix: never upgrade inside the band). Marks are written to the Parquet and saved (even in dry_run).
2. If no universe movies exist, `audit_universe_tags` runs to explain why.
3. `apply_quality_actions` loads the Parquet fresh, clears stale `quality_action` on rows no longer universe-tagged, reaps only **universe-authored** ledger stamps (`plan_reason ∈ {"universe upgrade","universe downgrade"}` — so it never clobbers space-pressure's bare-universe downgrade stamp), then per pending row: computes `watch_likelihood`, resolves the target profile, and PUTs (or in dry_run logs + stamps only).

Brain delegation (modules named, not documented here):
- `machine_learning.space.universe_quality` — `universe_action`, `upgrade_target`, `downgrade_target`, `downgrade_single_rank` (the up/down profile decisions).
- `machine_learning.ledger.decision_ledger.stamp_universe_plan` (the signed-reclaim ledger stamp).
- `support.utilities.watch_likelihood` — `watch_likelihood`, `profile_id_for_likelihood` (likelihood gating of the 4K upgrade tier).
- `support.utilities.space_targets.space_targets` and `support.utilities.space_floor_alert.alert_unconfigured_floor` (the shared band + floor-unset warning).

## Criteria & examples

- **Band thresholds**: downgrade when `free < T`; upgrade when `free > U`; hold (clear action) in `[T, U]`. Example: with `T = 100 GB`, `U = 110 GB`, at 90 GB free every universe movie is marked `"downgrade"`; at 130 GB free they are marked `"upgrade"`; at 105 GB free pending actions are cleared (no quality thrash under pressure).
- **Upgrade is likelihood-gated**: the 4K tier is reserved for rewatched content. A universe title with low watch-likelihood that is marked `"upgrade"` may resolve `target_profile is None` because it is "already at its earned tier" — e.g. logged as `likelihood=20% → profile <id> — no upgrade`. A rewatched title (high likelihood) can step up toward 4K.
- **Downgrade is one resolution tier**: `_downgrade_target` steps best-quality down one tier (4K → 1080p), runtime-sized; it no-ops a title already at its floor (logged "already at lowest eligible profile").
- **min_rank floor**: `min_rank=1` would prevent ever selecting the cheapest profile in the ranked list as a downgrade target.
- **Stale-tag guard**: if a movie like *Life of Pi* had a universe tag removed but still carries a `quality_action` from a prior run, it is cleared (logged as a stale non-universe row) and never applied.

## In plain English

Some movies are part of a box set you'd never throw away — the whole Marvel Cinematic Universe, say, or every Christopher Nolan Batman film. This manager treats those specially: it will never delete them. But when your hard drive gets full, instead of deleting an Avengers film it quietly swaps it to a smaller 1080p copy to save room; when space frees up again, it puts the 4K copy back — though it only bothers with the pricey 4K version for films the household actually rewatches. It also double-checks that the franchise tags in Radarr match what it has on record, and warns you if, say, you meant to tag the MCU but no "keep-universe-mcu" tag exists yet. In dry-run it only writes down what it *would* do.

## Interactions

- **Parent**: `RadarrQualityManager`.
- **Siblings**: `RadarrSpacePressureManager` (closely coupled — space-pressure downgrades a *bare* `universe` title as a last resort in the same run; the ledger-stamp scoping here is deliberately narrowed to universe-authored reasons so the two managers don't clobber each other's stamps), plus `RadarrQualityAdjustmentManager`, `RadarrCustomFormatsManager`, `RadarrFileSizesManager`, `RadarrQualitySelectorManager`.
- **Other managers**: `RadarrCacheMovieFilesManager` (the Parquet load/save provider, looked up via the registry); `RadarrManager` / `Main` (dry_run source).
- **Services**: `radarr_api`, `instance_manager`, `global_cache` (audit only).
- **Brain modules** (named, not documented): `machine_learning.space.universe_quality`, `machine_learning.ledger.decision_ledger`.
