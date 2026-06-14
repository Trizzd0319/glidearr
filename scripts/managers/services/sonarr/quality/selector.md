# SonarrQualitySelectorManager

- **File** — `scripts/managers/services/sonarr/quality/selector.py`
- **One-liner** — Picks the best quality profile for a series/episode (scoring custom-format points minus ML transcode penalties) and applies the chosen profile back to Sonarr, plus utilities to cache and normalize quality profiles. (Cross-instance sync utilities are moot under the single un-tiered `sonarr` instance.)

## What it does (for a senior Python engineer)

`SonarrQualitySelectorManager(BaseManager, ComponentManagerMixin)` is the FETCH + CACHE + APPLY decision-surface for quality profiles in the Sonarr quality tree. It is the only quality submanager that consults the ML brain.

Key public methods:
- `request_quality_change(series_title, season, episode, resolution, instance, decision)` → `bool`. APPLY: resolves the episode id, calls `compare_profiles_for_series` to pick a profile, then `PUT episode/{episode_id}` with `{"qualityProfileId": new_profile_id}`. Returns False (no mutation) if the episode or a suitable profile isn't found. The `resolution`/`decision` args are logged but not otherwise used to compute the target.
- `compare_profiles_for_series(instance, series_title, season, episode)` → `profile_id`. The scoring core: pulls profiles + custom-format scores + ML transcode penalties, filters via `_is_valid_profile`, computes `final_score = cf_score - penalty`, sorts by `(final_score, cf_count)` desc, logs a comparison table, and returns the top profile id (or the instance default if none qualify).
- `get_best_quality_profile_ai(instance, series_title)` → `profile_id`. Alternate scorer: `final_score = cf_score + ai_score` where `ai_score` comes from `ml_manager.predict_best_quality_profile`. Returns the highest-scoring profile id.
- `get_quality_profiles(instance)` → live `qualityprofile` list.
- `_is_valid_profile(profile_name, instance)` → `bool` profile-validity guard (see Criteria). It no longer infers a target resolution from the instance name; on the single un-tiered `sonarr` instance it accepts any real profile (including `Any`), and per-episode JIT governs the actual resolution.
- `run_quality_data_pull(instance)` → CACHE: serializes each instance's `arrapi_client.quality_profile()` and writes to global_cache.
- `get_next_quality(config, series_id, blacklisted)` → next quality from `quality_order` not in the blacklist; pure helper, no API.
- `sync_quality_profiles_across_instances()` → APPLY: builds a union of profile names across instances and `POST qualityprofile` for any missing on a given instance. (Moot under the single un-tiered `sonarr` instance — the union degenerates to one instance's own profiles.)
- `log_missing_profiles()` → logs which instances lack a profile common to all others. (Moot with N=1.)
- `normalize_profile_names()` → APPLY: `PUT qualityprofile/{id}` to rename profiles to `.strip().title()` casing where they differ.

Position in the tree: child of **SonarrQualityManager** (`self.parent_name = "SonarrQuality"`). Loads no submanagers. NOTE: several methods call through `self.manager` for `get_quality_profiles`, `get_custom_format_scores`, `get_default_quality_profile`, and `ml_manager` — so `self.manager` is expected to expose the broader Sonarr-quality surface (the parent or an aggregating manager), not just this leaf. `self.instance_manager` is also used in every method but is not assigned in `__init__` of this file (it is inherited via BaseManager auto-link / parent) — if absent, instance resolution would fail.

FETCH / CACHE / APPLY:
- FETCH: `_make_request("qualityprofile")`, `arrapi_client.quality_profile()`, `sonarr_api.get_episode_id(...)`.
- CACHE: `run_quality_data_pull` writes serialized profiles; `_is_valid_profile` memoizes profiles in `self._cached_profiles` (per-instance, in-memory).
- APPLY: `PUT episode/{id}` (profile change), `POST qualityprofile` (sync), `PUT qualityprofile/{id}` (rename).

External API endpoints touched (Sonarr REST via `_make_request` / arrapi client):
- `episode/{episode_id}` (`PUT`), `qualityprofile` (GET and `POST`), `qualityprofile/{id}` (`PUT`).

Config keys read: `ignore_resolution_check` (bool, skips resolution validation), `resolution_patterns` (dict mapping `"720"/"1080"/"2160"` → name substrings; defaults baked in), plus `quality_order` (read off the `config` argument in `get_next_quality`, default `["SD","720p","1080p","4K"]`).

global_cache keys written: `key_builder.format_cache_key("sonarr", instance_name, "quality_profiles")` — payload `{"qualityProfiles": [...], "meta": {...}}` via `set_with_pretty_output`. (Compare with `CacheKeyPaths.sonarr.QUALITY_PROFILES = "sonarr/<instance>/quality/profiles"`.)

dry_run: `self.dry_run` is captured but **not consulted** in any APPLY method (`request_quality_change`, `sync_quality_profiles_across_instances`, `normalize_profile_names` all mutate unconditionally) — flagging this as a dry_run gap; the "would ..." convention is not honored here.

Singleton / concurrency: standard `BaseManager` singleton. `_cached_profiles` is a lazily-created per-instance memo on the instance — no locking, single-threaded assumption.

## How it functions

Init: sets `parent_name = "SonarrQuality"`, `BaseManager.__init__`, sets up the dual cache (`sonarr_cache` from kwargs/manager and `global_cache`), `register()`, then resolves `sonarr_api`, `logger`, `manager`, `dry_run` from kwargs or the registry-resolved parent; raises without a logger; debug "Initialized" line.

Control flow for the central decision (`compare_profiles_for_series`):
1. `resolve_instance`.
2. `profiles = self.manager.get_quality_profiles(...)`, `cf_scores = self.manager.get_custom_format_scores(...)`, `penalties = self.manager.ml_manager.get_transcode_history(series_title)`.
3. For each profile passing `_is_valid_profile`, compute `cf_score = cf_scores.get(profile_id, 0)`, `penalty = penalties.get(series_title, 0)`, `final_score = cf_score - penalty`.
4. Sort `(final_score, cf_count)` descending; log a table; return `profile_data[0][0]` else `self.manager.get_default_quality_profile(...)`.

**ML delegation (do NOT document the brain):** the decision inputs come from `self.manager.ml_manager` — `get_transcode_history(series_title)` (penalty signal used by `compare_profiles_for_series`) and `predict_best_quality_profile(series_title)` (the `ai_score` used by `get_best_quality_profile_ai`). This selector only *fetches* those numbers and combines them with Sonarr's custom-format scores; the value-judgement lives in the `machine_learning` layer.

## Criteria & examples

- **`compare_profiles_for_series` scoring:** `final_score = cf_score − penalty`, ties broken by `cf_count`. Example: profile "1080p" has `cf_score = 80`, ML transcode `penalty = 30` → `final = 50`; profile "4K" has `cf_score = 60`, `penalty = 0` → `final = 60`. "4K" wins (60 > 50) and its id is `PUT` onto the episode.
- **`get_best_quality_profile_ai` scoring:** `final_score = cf_score + ai_score`. Example: "WEB-DL-1080p" `cf_score = 40`, `ai_score = 35` → 75 beats "HDTV-720p" at `cf_score 50 + ai 10 = 60`.
- **`_is_valid_profile` guards (in order):**
  - If config `ignore_resolution_check` is true → always valid.
  - Names `"default"`/`"unknown"` → invalid.
  - If the resolved instance name carries **no resolution marker** (none of `"4k"`/`"2160"`/`"1080"`/`"720"`) — as is the case for the single un-tiered `sonarr` instance — the profile is valid. Any real profile, including `"Any"`, is accepted; per-episode JIT governs the actual resolution rather than an instance-name → resolution gate. Example: every profile on instance `sonarr` is allowed.
  - (Legacy tiered behavior, retained only for an instance whose name *does* embed a resolution marker.) Name `"any"` → valid only if the instance name contains `"4k"` or `"2160"`. Otherwise the target resolution is `"2160"` if the instance contains `"4k"`, `"1080"` if `"1080"`, else `"720"`, and the profile must contain at least one `allowed` quality whose name matches a pattern from `resolution_patterns[target_res]` (defaults: `2160`→`["2160p","4k"]`).
- **`get_next_quality`:** with `quality_order = ["SD","720p","1080p","4K"]` and `blacklisted = ["SD","720p"]`, returns `"1080p"`; returns `None` if all are blacklisted.
- **`normalize_profile_names`:** `"web-dl 1080p"` → `"Web-Dl 1080P"` (`.strip().title()`); only renames when changed.

## In plain English

When you want a show like *Stranger Things* in the best version your setup can actually handle, this is the component that picks the recipe. It looks at how many "good-quality points" each available recipe earns (Sonarr's custom-format scores) and subtracts a penalty if past experience shows that recipe forces your media server to work too hard re-encoding it (the smart penalty from the ML brain). The recipe with the best net score wins, and it tells Sonarr to use it. On the single un-tiered Sonarr it accepts any real recipe (per-episode JIT decides the resolution), and it can also tidy up recipe names. The old cross-server chores — copying missing recipes from one server to another and refusing nonsense like a "4K-only" recipe on a 1080p-only server — are moot now that there is just one Sonarr instance.

## Interactions

- **Parent manager:** `SonarrQualityManager` (with several calls routed through `self.manager`, the broader Sonarr-quality surface).
- **Sibling submanagers:** `SonarrQualityAdjustmentManager`, `SonarrQualityCustomFormatsManager`, `SonarrQualityFileSizesManager`.
- **Services it talks to:** the Sonarr API adapter (`sonarr_api`) for profile reads, episode-id lookup, the profile-change `PUT`, and cross-instance sync/rename; `instance_manager` for resolution; `global_cache` + `key_builder` for the quality-profiles cache.
- **Brain modules (delegated decisions, not documented here):** `self.manager.ml_manager.get_transcode_history` and `self.manager.ml_manager.predict_best_quality_profile` — both live under `machine_learning/` and supply the penalty / AI scoring inputs.
