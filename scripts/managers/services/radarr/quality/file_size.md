# RadarrFileSizesManager

**File** — `scripts/managers/services/radarr/quality/file_size.py`
**One-liner** — Estimates the expected on-disk size of a movie from its quality profile and runtime, and classifies an actual file as upgrade / downgrade / keep.

## What it does (for a senior Python engineer)

`RadarrFileSizesManager(BaseManager, ComponentManagerMixin)` answers "how big should this movie be, and is the file we have too big/too small?" It is FETCH-only against Radarr (GET qualityprofile, GET movie) — it never writes. The size math and the keep/upgrade/downgrade verdict are delegated to a `machine_learning` brain module.

Class attribute:
- `QUALITY_MB_PER_MIN = size_model.CALIBRATED_MB_PER_MIN` — the shared, library-calibrated MiB/min table. The docstring records that this replaced an older per-quality dict whose 1999/2000 MB/min ceilings produced absurd ~187 GB estimates for 90-minute movies.

Public methods:
- `get_expected_file_size(instance, profile_id, runtime_minutes) -> float` — Looks up the profile name, calls `size_model.mb_per_min(profile_name)`, returns `mb_per_min * runtime_minutes * 1024**2` (bytes).
- `get_median_file_size(instance, profile_id) -> float` — Empirical estimate: GETs all `movie`, collects `sizeOnDisk` for movies matching `profile_id` that `hasFile` and have `sizeOnDisk > 0`, returns the median (`sorted(sizes)[len//2]`). Falls back to `get_predefined_file_size(..., runtime_minutes=120)` when there are no samples.
- `get_predefined_file_size(instance, profile_id, runtime_minutes=120) -> float` — Same math as `get_expected_file_size` but logs the profile name + MiB/min for auditing.
- `compare_file_size(instance, movie_id, actual_bytes) -> str` — FETCHes the movie, computes expected size (using `runtime` or default 90), then **delegates the verdict** to `classify_file_size(actual_bytes, expected)`. Returns `"upgrade"`, `"downgrade"`, or `"keep"`. Returns `"keep"` if the movie can't be fetched.
- `generate_quality_flags(instance) -> dict` — GETs all `movie`, returns `{movie_id: {"title", "issues"}}` for movies that either have `qualityCutoffNotMet` true or are `monitored` without a file. Issue strings: `"quality_cutoff_not_met"`, `"missing_file"`.

Internal helpers:
- `_resolve_instance(instance)` — standard instance resolution.
- `_get_profile_name(instance, profile_id) -> str` — GETs `qualityprofile`, returns the matching profile's name (`"Unknown"` if not found).

- **Parent manager**: `RadarrQualityManager`.
- **Submanagers loaded**: none.
- **External API endpoints**: `GET qualityprofile`, `GET movie`, `GET movie/{id}`.
- **config keys read**: none.
- **global_cache / Parquet keys**: none read or written.
- **dry_run**: `self.dry_run` is resolved but unused (this manager performs no writes).
- **Singleton / concurrency**: standard `BaseManager` singleton; no threading.

## How it functions

`__init__` does standard wiring. No `load_components`, no top-level `run()`. The estimators are pure FETCH + arithmetic against the calibrated MiB/min model.

Brain delegation:
- Size-per-minute lookup: `scripts.managers.machine_learning.sizing.size_model` (`mb_per_min`, `CALIBRATED_MB_PER_MIN`).
- Keep/upgrade/downgrade decision: `scripts.managers.machine_learning.sizing.file_comparison.classify_file_size`. The service deliberately keeps only the Radarr fetch + expected-size estimate; the value judgement lives in the brain. (These ML modules are out of scope to document here — named only.)

## Criteria & examples

- **Expected-size formula**: `mb_per_min(profile_name) * runtime_minutes * 1024**2` bytes. Example: a profile whose calibrated rate is 50 MiB/min, for a 120-minute film → `50 * 120 * 1024**2 ≈ 6.29 GB`.
- **Median fallback**: if a profile has zero sampled movies on disk, `get_median_file_size` falls back to the predefined estimate at a 120-minute assumption. If samples exist, the median element is taken at index `len(sizes)//2` of the sorted list (so for an even count it picks the upper-middle element, not a true interpolated median).
- **compare_file_size**: the verdict itself is the brain's; this method only feeds it `actual_bytes` and the computed `expected`. A movie with no fetchable record short-circuits to `"keep"`.
- **generate_quality_flags**: a monitored movie with no file → `{"issues": ["missing_file"]}`; a movie Radarr marks `qualityCutoffNotMet` → `{"issues": ["quality_cutoff_not_met"]}`; both can appear together.

## In plain English

This manager is the "is this file the right size?" inspector. It knows, from measuring your real library, roughly how many megabytes a minute of a 1080p or 4K film should take, so it can say "a two-hour movie in this quality ought to be about 6 GB." It can also look at every movie sharing a quality level and use the typical (median) size as the yardstick. Then, for a given movie, it hands the expected size and the actual size to a separate decision-maker that says keep it, upgrade it, or downgrade it — much like a clerk who measures a parcel and lets the supervisor decide whether to re-ship it. It can also produce a checklist of movies that are missing their file or stuck below their target quality.

## Interactions

- **Parent**: `RadarrQualityManager`.
- **Siblings**: `RadarrQualityAdjustmentManager`, `RadarrCustomFormatsManager`, `RadarrQualitySelectorManager`, `RadarrSpacePressureManager`, `RadarrQualityUniverseManager`. (The size model here is the shared source of truth that space-pressure downgrade/upgrade planning also relies on.)
- **Services**: `radarr_api`, `instance_manager`/`radarr_api.resolve_instance`.
- **Brain modules** (named, not documented): `machine_learning.sizing.size_model`, `machine_learning.sizing.file_comparison.classify_file_size`.
