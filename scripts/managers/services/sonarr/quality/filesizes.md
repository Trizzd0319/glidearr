# SonarrQualityFileSizesManager

- **File** — `scripts/managers/services/sonarr/quality/filesizes.py`
- **One-liner** — Estimates the expected size of TV episode files and compares them to the real on-disk sizes to flag episodes for upgrade, downgrade, or keep.

## What it does (for a senior Python engineer)

`SonarrQualityFileSizesManager(BaseManager, ComponentManagerMixin)` is a FETCH + (occasional) CACHE leaf submanager under the Sonarr quality tree. It performs no PUT/DELETE/POST itself — all methods are read-only against Sonarr plus arithmetic against a calibrated size table.

Class-level data:
- `QUALITY_MB_PER_MIN = size_model.CALIBRATED_MB_PER_MIN` — the shared, library-calibrated MiB-per-minute table from `scripts/support/utilities/size_model.py` (single source of truth for size estimation).

Key public methods:
- `compare_file_sizes(rating_key, instance)` → `"upgrade" | "downgrade" | "keep"`. Gets actual size via `sonarr_api.get_episode_file_size(rating_key, resolved_instance)`, gets expected via `self.estimate_expected_size(...)`, applies a 0.6×/1.4× band. NOTE: this method is **defined twice** in the file (lines 46 and 168) — the second definition wins. Both call `self.estimate_expected_size`, which is **not defined in this class**; calling either method would raise `AttributeError` unless `estimate_expected_size` is supplied elsewhere — flagging this as a likely latent bug.
- `get_expected_file_size(instance, quality_profile_id, runtime)` → size in **bytes**. Resolves the profile name, looks up `size_model.mb_per_min(profile_name)`, returns `mb_per_min * runtime * 1024**2`.
- `get_median_file_size(instance, quality_profile_id, runtime)` → median real episode size in bytes. Calls `episode?seriesId={quality_profile_id}` (note: the arg named `quality_profile_id` is used here as a **seriesId**), takes the middle element of sorted positive `size` values; falls back to `get_expected_file_size` if no data.
- `get_predefined_file_size(instance, quality_profile_id, runtime)` → same arithmetic as `get_expected_file_size` but with an info log of the per-minute rate.
- `generate_quality_flags(instance)` → `{episodeId: {"issues": [...], "count": n}}`. Flags `nonstandard_quality` (has a file but quality name not in `["HD-1080p","WEB-DL-1080p"]`) and `missing_file` (monitored but no file).
- `run_quality_definition_data_pull(instance)` → CACHE step: for every Sonarr instance, raw-HTTP `GET {base_url}/api/v3/qualitydefinition` and write the result to global_cache.
- `compare_codecs(episode_data, expected_codec="x265")` → `"match" | "unknown" | "mismatch (<codec>)"` by comparing `mediaInfo.videoCodec`.
- `flag_size_anomalies(instance, series_id, threshold_percent=50)` → list of `{"episodeId", "type": "too_small"|"too_large", "ratio"}`.
- `summarize_quality_distribution(instance)` → `{quality_name: count}` sorted descending.
- `get_average_size_by_quality(instance)` → `{quality_name: avg_MB}` (rounded MB).
- `get_flagged_upgrades_or_downgrades(instance)` → walks all series + episodes and buckets each `episodeFileId` into `upgrade`/`downgrade`/`keep` via `compare_file_sizes`.

Position in the tree: child of **SonarrQualityManager** (derives `self.parent_name` by stripping the `"Manager"` suffix → `"SonarrQualityFileSizes"`; then re-derives `"SonarrQuality"`-style lookup via the registry parent fetch). It loads no submanagers itself.

FETCH / CACHE / APPLY:
- FETCH: `sonarr_api._make_request(...)` against `episode`, `episode?seriesId=...`, `episodes`, `qualityprofile`, plus `sonarr_api.get_episode_file_size`, `get_all_sonarr_apis`, and a raw `requests.get`.
- CACHE: only `run_quality_definition_data_pull` writes.
- APPLY: none.

External API endpoints touched:
- Sonarr REST via `_make_request`: `episode`, `episode?seriesId={id}`, `episodes`, `qualityprofile`.
- Raw HTTP: `GET {base_url}/api/v3/qualitydefinition` with header `X-Api-Key`.

Config keys read: `sonarr_instances` (then per-instance `base_url` and `api`).

global_cache keys written: `key_builder.format_cache_key("sonarr", instance_name, "quality_definitions")` — payload `{"qualityDefinitions": [...], "meta": {timestamp, instance, count}}`, via `global_cache.set_with_pretty_output`.

dry_run: `self.dry_run` is captured (kwarg, else parent `manager.dry_run`, else False) but is **not referenced** anywhere in the methods — none of them mutate Sonarr, so there is nothing to guard. Effectively a no-op flag here.

Singleton / concurrency: standard `BaseManager` singleton; no threading.

## How it functions

Init: standard `BaseManager.__init__` + `self.register()`, then it pulls `sonarr_api`, `logger`, `manager`, `instance_manager`, `sonarr_cache`, `global_cache`, `key_builder`, `dry_run` from kwargs or fall back to the registry-resolved parent. Raises if no logger. Logs a debug "Initialized" line.

Main control flow is request/response per method — there is no single `run()`. Every method first calls `self.instance_manager.resolve_instance(instance)` to normalize the instance handle, then queries Sonarr and does arithmetic against the `size_model` table.

No `machine_learning` brain module is invoked by this file. Size estimation funnels through `size_model.mb_per_min` (see the project's unified size model), not a brain decision.

## Criteria & examples

- **Upgrade/downgrade band (`compare_file_sizes`):** `actual == 0` OR `actual < expected * 0.6` → `"upgrade"`; `actual > expected * 1.4` → `"downgrade"`; else `"keep"`. Worked example: a 45-minute episode whose profile estimates 1,000 MB expected. If the file is 500 MB (< 600 MB = 0.6×) it's an upgrade candidate; if it's 1,600 MB (> 1,400 MB = 1.4×) it's a downgrade candidate; 900 MB is keep.
- **Nonstandard quality (`generate_quality_flags`):** an episode with a file whose quality name is, say, `"SDTV"` (not `"HD-1080p"` or `"WEB-DL-1080p"`) earns the `nonstandard_quality` issue.
- **Size anomaly (`flag_size_anomalies`, threshold 50%):** expected 1,000 MB. Actual 400 MB → ratio 0.40 < 0.50 → `too_small`. Actual 1,700 MB → ratio 1.70 > 1.50 → `too_large`. Episodes with no `quality.quality.id`, no file, or zero size are skipped; default runtime is 45 min.
- **Codec (`compare_codecs`):** expected `"x265"`; an episode reporting `videoCodec="x264"` returns `"mismatch (x264)"`; empty codec returns `"unknown"`.

## In plain English

Imagine you expect a single 45-minute episode of a show like *The Mandalorian* in good HD to weigh about 1 GB. This component is the bathroom scale for episode files. If an episode weighs way too little (under 60% of expected) it probably looks bad and is worth re-grabbing in better quality ("upgrade"). If it weighs way too much (over 140%) it's hogging disk for no benefit and could be slimmed down ("downgrade"). It can also spot oddballs — an episode encoded with the "wrong" video codec, or one that's missing entirely — so the library stays consistent. It only measures and reports; it never deletes or replaces anything on its own.

## Interactions

- **Parent manager:** `SonarrQualityManager`.
- **Sibling submanagers:** `SonarrQualityAdjustmentManager`, `SonarrQualityCustomFormatsManager`, `SonarrQualitySelectorManager`.
- **Services it talks to:** the Sonarr API adapter (`sonarr_api`) for episode/profile/quality-definition reads; `instance_manager` for instance resolution; `global_cache` for the quality-definitions cache write; `size_model` for calibrated MiB/min estimates.
- **Brain modules:** none.
