# SonarrEpisodesRetrievalEnrichmentManager

**File** — `scripts/managers/services/sonarr/episodes/retrieval/enrich.py`
**One-liner** — Decorates raw Sonarr episode dicts in place with series titles, standardized key aliases, format/quality summaries, and per-file metadata (size, codec, audio, release group), plus simple quality-distribution and missing-file summaries.

## What it does (for a senior Python engineer)

A FETCH/transform submanager. It takes lists of episode dicts and enriches them by joining against Sonarr series data and the cached episode-file table. It declares `parent_name = "SonarrEpisodes"` (note: not `"SonarrEpisodesRetrieval"`), so its registry parent resolution targets the `SonarrEpisodes` manager.

Public methods:
- `enrich_with_series_title(episodes, instance)` — builds `{series_id: title}` from `sonarr_api.get_all_sonarr_apis()[resolved].all_series()` and sets `ep["seriesTitle"]` (defaulting to `"Unknown"`).
- `enrich_with_format_summary(episodes)` — sets `ep["format"] = "<quality> / <video> / <audio>"` from `ep["quality"]["quality"]["name"]`, `ep["mediaInfo"]["videoCodec"]`, `ep["mediaInfo"]["audioCodec"]`.
- `apply_standardized_keys(episodes)` — adds flat aliases: `ep["episode"]=episodeNumber`, `ep["season"]=seasonNumber`, `ep["series"]=seriesId`.
- `merge_enrichments(episodes, instance)` — convenience pipeline: standardized keys → series title → format summary, returned in that order.
- `enrich_episode_data(episodes, instance)` — joins each episode against the cached episode-file table (`sonarr_cache.episodes.get_all_episode_data(resolved)`) by `episodeFileId`, returning **new** dicts (`{**ep, ...}`) carrying `fileSize`, `quality`, `codec`, `audio`, `sceneName`, `releaseGroup`.
- `summarize_quality_distribution(instance) -> dict` — counts episode files by quality-profile name, sorted descending by count.
- `find_episodes_missing_file(episodes) -> list` — returns episodes where `hasFile` is falsy or `episodeFileId` is missing.

External API endpoints touched: indirectly via `sonarr_api.get_all_sonarr_apis()[...].all_series()` (Sonarr series listing). No direct `_make_request`.

FETCH / CACHE / APPLY: FETCH/transform only — it mutates the passed-in dicts (or returns derived copies) but never persists to cache and never APPLYs to Sonarr.

Config keys read: none directly.
global_cache / Parquet keys: reads episode-file data through `sonarr_cache.episodes.get_all_episode_data(...)`; writes nothing.

dry_run: not referenced (no mutating I/O).

Concurrency: `BaseManager` singleton, no threading.

## How it functions

`__init__` calls `super().__init__` + `register()`, then resolves `self.manager` from kwargs or `registry.get("manager", "SonarrEpisodes")`, and pulls `sonarr_api`, `global_cache`, `sonarr_cache` (from `kwargs["cache_manager"]`), and `instance_manager` from kwargs or the parent.

`merge_enrichments` is the canonical entry that chains the three in-place enrichers. `enrich_episode_data` is a separate, heavier join that builds a `{file_id: file_info}` lookup once and produces enriched copies — it does not mutate the inputs. The two summary methods (`summarize_quality_distribution`, `find_episodes_missing_file`) are read-only reporting helpers.

No decision is delegated to a `machine_learning` brain module.

## Criteria & examples

- **Format string**: an episode at quality "WEBDL-1080p" with `videoCodec="x265"` and `audioCodec="EAC3"` gets `ep["format"] = "WEBDL-1080p / x265 / EAC3"`.
- **Missing-file rule** (`find_episodes_missing_file`): an episode with `hasFile=False` (or `episodeFileId` absent/0) is returned as missing. Example: a monitored episode of Andor that aired but hasn't been grabbed (`hasFile=False`) is flagged; a fully-imported one (`hasFile=True, episodeFileId=552`) is not.
- **Series-title default**: if an episode's `seriesId` is not in the series map (e.g. a stale/removed series), `ep["seriesTitle"]` becomes `"Unknown"`.
- **Quality distribution**: for a library of 100 files — 70 "Bluray-1080p", 25 "WEBDL-720p", 5 untagged — returns `{"Bluray-1080p": 70, "WEBDL-720p": 25, "Unknown": 5}` (descending).

## In plain English

Sonarr hands over episode records that are technically complete but bare-bones — IDs and numbers, not friendly names. This manager is the copy-editor that fleshes them out: it stamps each episode with its show's actual title (so "series 88, episode 4821" becomes "The Last of Us — Long, Long Time"), writes a tidy one-line spec like "Bluray-1080p / x265 / EAC3", and looks up file details like size and release group. It can also produce a quick tally ("you have 70 Blu-ray-quality files, 25 web-quality") and a list of episodes you're missing the actual video file for. It only annotates; it never downloads or deletes anything.

## Interactions

- **Parent manager:** resolves against `SonarrEpisodes` (declared `parent_name`), under the `SonarrEpisodesRetrieval` subtree.
- **Siblings:** `fetch`, `tvdb`, `sync`, `validate`, `episode_cache`.
- **Services it talks to:** Sonarr API (`sonarr_api.get_all_sonarr_apis()`/`all_series()`); the Sonarr cache (`sonarr_cache.episodes.get_all_episode_data`); `instance_manager` for resolution.
- **Brain modules:** none.
