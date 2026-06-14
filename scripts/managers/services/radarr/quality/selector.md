# RadarrQualitySelectorManager

**File** — `scripts/managers/services/radarr/quality/selector.py`
**One-liner** — Picks, validates, and assigns the right Radarr quality profile for a movie, based on the instance's resolution tier and cached custom-format scores.

## What it does (for a senior Python engineer)

`RadarrQualitySelectorManager(BaseManager, ComponentManagerMixin)` resolves "which quality profile should this movie use?" It performs FETCH (GET qualityprofile, GET/PUT movie) and APPLY (PUT a movie with a new `qualityProfileId`). It keeps an in-process per-instance profile cache in `self._cached_profiles` (a plain dict, not `global_cache`).

Public methods:
- `get_quality_profiles(instance) -> list` — FETCH + memoize. Returns `self._cached_profiles[resolved]` if present; otherwise GETs `qualityprofile`, memoizes, returns.
- `get_default_quality_profile(instance) -> int` — Returns the first profile's `id` (fallback `1` when none exist).
- `request_quality_change(movie_id, instance, profile_id) -> bool` — APPLY. GETs `movie/{movie_id}`; if missing returns False. **dry_run aware**: logs `[dry_run] Would set quality profile ...` and returns True without writing. Otherwise sets `movie["qualityProfileId"]` and PUTs `movie/{movie_id}`.
- `assign_default_profile_if_missing(movie_data, instance) -> dict` — If `movie_data` has no `qualityProfileId`, fills in `get_default_quality_profile`. Pure dict mutation (no API write).
- `get_best_profile_for_instance(instance) -> int` — Selection. Walks all valid profiles, scores each by a cached custom-format score map, returns the best profile id (fallback: default profile).

Internal helpers:
- `_resolve_instance(instance)` — standard instance resolution.
- `_is_valid_profile(profile_name, instance) -> bool` — resolution-tier validation (see Criteria).

- **Parent manager**: `RadarrQualityManager`.
- **Submanagers loaded**: none.
- **External API endpoints**: `GET qualityprofile`, `GET movie/{id}`, `PUT movie/{id}`.
- **config keys read**: `ignore_resolution_check` (bool, default False), `resolution_patterns` (dict, default `{"720":["720p"],"1080":["1080p"],"2160":["2160p","4k"]}`).
- **global_cache keys**: reads `radarr.quality.{resolved}` — expected shape `{profile_name: score}` (the custom-format score map). Never writes.
- **dry_run**: enforced in `request_quality_change`.
- **Singleton / concurrency**: standard `BaseManager` singleton; `_cached_profiles` is per-instance process memory.

## How it functions

`__init__` does standard wiring plus initializes `self._cached_profiles = {}`. No `load_components`, no top-level `run()`.

`get_best_profile_for_instance` is the heart: it fetches profiles, pulls the cached `{name: score}` map from `global_cache["radarr.quality.{resolved}"]`, skips profiles that fail `_is_valid_profile`, and keeps the one with the highest score (ties keep the first seen because the comparison is strict `>`). If nothing qualifies it falls back to the default profile id.

No `machine_learning` brain module is involved — validation and best-pick are local heuristics, not delegated decisions.

## Criteria & examples

`_is_valid_profile` rules, in order:
1. Empty name or instance → `False`.
2. `config["ignore_resolution_check"]` true → `True` (skip all checks).
3. Name in `{"default", "unknown"}` → `False` (fallback names rejected).
4. Name `"any"` → `True` only if the resolved instance name contains `"4k"` or `"2160"`, else `False`.
5. Otherwise: derive the target resolution from the instance name (`"2160"` if `"4k"` in name, else `"1080"` if `"1080"` in name, else `"720"`), look up the matching profile by name, and accept it only if it has an `allowed` quality item whose name contains one of the target resolution's patterns.

Worked example: instance `"radarr-4k"`, profile named `"UHD Bluray"`. Target resolution = `"2160"`, patterns `["2160p","4k"]`. If that profile has an allowed item named `"Bluray-2160p"`, `_is_valid_profile` returns True. Then in `get_best_profile_for_instance`, if the cached scores are `{"UHD Bluray": 120, "HD-1080p": 60}`, `"UHD Bluray"` wins (120 > 60) and its id is returned.

Worked example 2: instance `"radarr"` (no 4k/1080 token) → target `"720"`. A profile `"Any"` would only pass via rule 4 if the instance name signaled 4K; here it does not, so `"any"` → False.

## In plain English

When a movie arrives, somebody has to decide "do we keep it in 4K, 1080p, or 720p?" This manager is that decider. It first checks that a profile actually matches the shelf it lives on — a "4K shelf" instance should use a profile that genuinely allows 4K, not just one named "Any". Then, among the valid options, it picks the one your custom-format preferences score highest. For example, on your 4K Radarr it would pick "UHD Bluray" over "HD-1080p" because your rules give the UHD profile a higher score. In dry-run it just says "I would switch this movie to that profile" rather than actually changing it.

## Interactions

- **Parent**: `RadarrQualityManager`.
- **Siblings**: `RadarrQualityAdjustmentManager`, `RadarrCustomFormatsManager` (produces the kind of per-format scores that feed the cached `{name: score}` map), `RadarrFileSizesManager`, `RadarrSpacePressureManager`, `RadarrQualityUniverseManager`.
- **Services**: `radarr_api`, `instance_manager`/`radarr_api.resolve_instance`, `global_cache` (reads `radarr.quality.{instance}`).
- **Brain modules**: none.
