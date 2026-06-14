# SonarrRepairQualityManager

**File** — `scripts/managers/services/sonarr/repair/quality.py`
**One-liner** — Compares each Sonarr series' assigned quality profile against a configured expected profile (keyed by folder path) and corrects mismatches by reassigning the profile.

## What it does (for a senior Python engineer)

`SonarrRepairQualityManager(BaseManager, ComponentManagerMixin)` is a leaf repair sub-manager under `SonarrRepairManager`. It performs **FETCH** (profiles + series) and **APPLY** (update a series' quality profile).

- **Parent:** `self.parent_name = "SonarrRepair"`. Constructed by `SonarrRepairManager` (non-critical).
- **Deps:** `sonarr_api` from `sonarr_api`/`api` kwargs or the registered parent's `api` attr (raises `ValueError` if unresolved); `manager` and `dry_run` (kwarg or manager).
- **Loads submanagers:** none.

Public method:

- **`repair_quality_profiles()`** — for each instance in `get_all_sonarr_apis()`, reads `self.config.get("sonarr", {}).get("expected_quality_profiles", {})` (an expected map keyed by lowercased series path), builds `profile_map` = `{profile.id: profile.name}` from `api.profiles.all()`, and the inverse `expected_map` = `{name: id}`. For each series in `api.series.all()`, it looks up the `expected` profile name for `series.path.lower()` and the `actual` name for `series.qualityProfileId`. If both exist and differ, it either logs (dry-run) or sets `series.qualityProfileId = expected_map[expected]` and calls `api.series.update(series)`, incrementing `repaired`. Everything else increments `skipped`. Returns `None`.

- API endpoints touched (via the client): `profiles.all`, `series.all`, `series.update`.
- Config keys read: `sonarr.expected_quality_profiles` (a dict mapping lowercased series path → expected profile name).
- global_cache keys: none.
- FETCH / CACHE / APPLY: **FETCH + APPLY**.
- dry_run: when true, logs the mismatch with a `[dry-run]` suffix and makes no change.
- Singleton/threading: standard `BaseManager` singleton; no threading.

## How it functions

Lifecycle: `__init__` sets `parent_name`, calls `super().__init__`, `self.register()`, resolves the API + `dry_run`, raises if no API, logs an init line. The single public method is a per-instance, per-series reconciliation against the configured expectations, wrapped in a `try/except` per instance. A mismatch is only acted on when an `expected` name is configured for that path **and** an `actual` profile resolves **and** they differ; an unknown expected name (no matching id) is logged as an error and skipped. No `machine_learning` brain module is involved — the desired profiles come from config.

## Criteria & examples

- **Mismatch trigger:** `expected and actual and expected != actual`. Example: config maps `"/data/tv/anime/show"` → `"HD-1080p Anime"`, but the series at that path currently uses `"SD"` → mismatch → (unless dry-run) `qualityProfileId` is reset to the id of `"HD-1080p Anime"` and `api.series.update(series)` is called; `repaired += 1`.
- **No expected entry / match:** if no expected profile is configured for the path, or expected == actual, the series is counted in `skipped` with no change.
- **Unknown expected name guard:** if the configured expected name has no id in the instance, it logs `❌ Unknown expected profile name '<name>'` and increments `skipped` rather than applying.

## In plain English

In Sonarr, each show has a "how good a copy do I want?" setting — DVD-quality, 1080p, 4K, and so on. You can write down in the config what each show's setting *should* be (based on where it's filed). This specialist checks every show against that wish-list: if a show is set to plain DVD quality but your list says it should be 1080p, it switches it to 1080p. If you haven't specified a preference, or it already matches, it leaves it alone. In practice mode it just points out the mismatches without changing them.

## Interactions

- **Parent manager:** `SonarrRepairManager`.
- **Siblings:** the other `SonarrRepair*Manager` specialists.
- **Services:** the Sonarr per-instance API clients (`sonarr_api`); reads the `sonarr.expected_quality_profiles` config map.
- **Brain modules:** none (desired profiles are config-driven, not ML-derived).
