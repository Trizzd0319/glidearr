# SonarrEpisodesRetrievalValidationManager

**File** — `scripts/managers/services/sonarr/episodes/retrieval/validate.py`
**One-liner** — Cross-checks a list of episodes against every configured Sonarr instance to find ones with no file anywhere, and — when missing — suggests the next viable quality (skipping blacklisted qualities and "keep"-tagged series) before reporting the truly-unavailable ones.

## What it does (for a senior Python engineer)

A FETCH/analysis submanager. Given an `episode_list`, it determines which episodes are absent across **all** Sonarr instances and, for those, whether a fallback quality upgrade is worth suggesting. It returns the episodes for which no viable fallback exists (the "really missing" set).

Public method:
- `identify_missing_episode_files(episode_list) -> list` — the core routine. For each episode it:
  1. Skips series tagged `keep` (via `_is_keep_series`).
  2. Scans every configured instance's `episodefile` list; if the episode's `id` appears as an `episodeId`, it's "present somewhere" and skipped.
  3. For still-missing episodes, gathers blacklisted qualities across all instances (`blacklist?seriesId=&season=&episode=`).
  4. Asks `SonarrQualitySelectorManager.get_next_quality(config, series_id, blacklisted)` for a fallback; logs a suggested upgrade if one is returned, otherwise appends the episode to `missing_final`.

Private helper:
- `_is_keep_series(series_id) -> bool` — across all instance names, GETs `series/<series_id>` and returns `True` if any instance tags it `keep` (case-insensitive).

Note: `identify_missing_episode_files` constructs its own `SonarrAPI(logger=..., config=..., sonarr_instances=...)` instance from `config["sonarr_instances"]` rather than reusing the injected `self.sonarr_api`; `_is_keep_series` uses the injected `self.sonarr_api`.

External API endpoints touched (Sonarr): `episodefile`, `blacklist?seriesId=<sid>&season=<sn>&episode=<en>`, `series/<series_id>`.

FETCH / CACHE / APPLY: FETCH/analysis only. Despite the upgrade "suggestions," it performs **no** APPLY — it merely logs a suggested quality and returns the unrecoverable episodes. Triggering an actual search/upgrade is left to a higher layer.

Config keys read: `sonarr_instances` (both `config.get("sonarr_instances", {})` and used to build the `SonarrAPI`).
global_cache / Parquet keys: none — it queries Sonarr live.

dry_run: not referenced (no mutations to gate).

Concurrency: `BaseManager` singleton; live per-instance loops, no threading.

## How it functions

`__init__` resolves `self.manager`, `sonarr_api`, `instance_manager`, and the dual caches. `identify_missing_episode_files` is the entry point and bails early (returns `[]`) if no instances are configured. The control flow is a nested scan: outer loop over episodes, inner loops over instances for presence and blacklist checks. The quality-fallback decision is delegated to `SonarrQualitySelectorManager.get_next_quality(...)` (a sibling Sonarr quality manager, not a brain module).

No decision is delegated to a `machine_learning` brain module.

## Criteria & examples

- **Keep-tag guard:** if series 88 carries the `keep` tag on any instance, all its episodes are skipped (logged `🔒 Skipping validation for 'keep' series ID 88`) and never reported missing.
- **Present-anywhere guard:** episode id 4821 missing on the "anime" instance but present on "series" (its id shows up in that instance's `episodefile`) is treated as present and skipped.
- **Fallback decision:** episode 4821 (S2E5) is missing everywhere and "Bluray-2160p" + "Remux-2160p" are blacklisted. If `get_next_quality` returns "WEBDL-1080p", the manager logs `🔁 Suggest upgrading episode 4821 to: WEBDL-1080p` and does **not** add it to the returned list. If `get_next_quality` returns nothing (all viable qualities exhausted/blacklisted), it logs `❌ No viable quality fallback found for 4821 (2x5)` and appends the episode to `missing_final`.

## In plain English

This is the app's "is anything actually missing, and can we still get it?" inspector. For each episode you hand it, it checks every copy of Sonarr you run to see if the video file exists *anywhere*. If a show is marked "keep" (a permanent favorite), it doesn't bother — those are protected. For genuinely-missing episodes, it looks at which download qualities have already been ruled out (blacklisted) and asks the quality expert "is there a still-acceptable quality left to try?" If yes, it notes "we could grab it in 1080p web instead"; if no good option remains, it reports that episode as truly unavailable. Think of an episode of The Bear that never downloaded in 4K and got blacklisted — this tells you whether settling for 1080p is still on the table, or whether you're simply out of luck.

## Interactions

- **Parent manager:** `SonarrEpisodesRetrieval` (`SonarrEpisodesRetrievalManager`).
- **Siblings:** `fetch`, `enrich`, `tvdb`, `sync`, `episode_cache`.
- **Other Sonarr managers:** `SonarrQualitySelectorManager.get_next_quality(...)` (quality-fallback decision); a freshly-built `SonarrAPI` (from `arrapi`) plus the injected `self.sonarr_api`.
- **Services it talks to:** Sonarr API (`episodefile`, `blacklist`, `series`); `instance_manager` for instance names/resolution.
- **Brain modules:** none.
