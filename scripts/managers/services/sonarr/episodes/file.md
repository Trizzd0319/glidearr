# SonarrEpisodesFileManager

- **File** — `scripts/managers/services/sonarr/episodes/file.py`
- **One-liner** — Read-only inspector of Sonarr episode *files*: pulls episode-file metadata per series/instance, summarizes quality/codec/audio formats, warms a per-series file cache, and surfaces analytical candidates (orphans, codec drift, dedup suggestions).

## What it does (for a senior Python engineer)

`SonarrEpisodesFileManager(BaseManager, ComponentManagerMixin)` (`parent_name = "SonarrEpisodes"`) is the file-analytics child of `SonarrEpisodesManager`. It is almost entirely **FETCH** (HTTP GET against Sonarr's `episodefile` endpoints) plus in-memory aggregation; it performs no writes/deletes itself.

`__init__` resolves `self.manager`, `self.sonarr_api`, `self.instance_manager`, the dual cache (`self.global_cache` + `self.sonarr_cache`), and `self.dry_run` (from kwargs or parent). Raises `ValueError` if no logger.

Key public methods (most are decorated with `@timeit(...)`):

- `resolve_instance(instance)` — delegates to `instance_manager.resolve_instance(instance)`; returns the canonical instance name used in all API calls.
- `get_episode_file_size(episode_id, instance)` — GET `episodefile?episodeId={id}`; returns the summed `size` over returned dict files (int bytes).
- `get_episode_files_for_series(series_id, instance)` — GET `episodefile?seriesId={id}`; returns the list (or `[]`).
- `get_episode_file_metadata(instance)` — GET `episodefile` (all files for the instance); returns list (fallback `[]`).
- `get_episode_format_data(instance)` — maps each file to `{id, quality, codec, audio}` pulled from `quality.quality.name`, `mediaInfo.videoCodec`, `mediaInfo.audioCodec`.
- `get_codec_summary(instance)` — returns a dict keyed by `(quality, codec, audio)` → count, aggregated across all files of the instance.
- `get_format_counts_by_series(instance)` — enumerates all series (`get_all_sonarr_apis()[resolved].all_series()`), then per series counts `(series_id, quality, videoCodec, audioCodec)` → count.
- `warm_episode_file_cache_with_tqdm(series_list, instance)` — threaded warm-up: filters series with an `id`/`seriesId`, fans out `get_episode_files_for_series` over a `ThreadPoolExecutor(max_workers=5)`, shows a tqdm bar, and returns an in-memory dict `{series_id: [files]}`. (This is an in-process cache dict it returns — it does NOT persist to global_cache.)
- `find_orphaned_episode_files(instance)` — returns files whose `episodeId` is not in the set of known episode ids (`all_episodes()`); i.e. dangling files.
- `detect_codec_drift_by_season(instance, series_id)` — returns `{season: {codec: count}}` only for seasons that contain **more than one** distinct video codec.
- `suggest_codec_standardization(instance)` — returns `{"suggested_format": <most common (quality,codec,audio) key>, "count": n}` (the modal format), or `{}` if no data.
- `recommend_episode_deletion_candidates(instance, min_quality="SD")` — returns files whose `quality.quality.name == min_quality` (exact-match filter, default `"SD"`).

- Position in the tree: parent `SonarrEpisodesManager`; loads no submanagers.
- FETCH: yes (all `episodefile` GETs and `all_series`/`all_episodes` reads). CACHE: no persistent cache writes (only the returned in-memory dict in `warm_...`). APPLY: none.
- API endpoints (via `sonarr_api._make_request`): `episodefile`, `episodefile?episodeId={id}`, `episodefile?seriesId={id}`; plus `arrapi` `all_series()` / `all_episodes()` via `get_all_sonarr_apis()[resolved]`.
- Config keys: none read directly.
- global_cache / Parquet keys: none read/written.
- dry_run: stored but unused (no mutating method here).
- Concurrency: `warm_episode_file_cache_with_tqdm` uses a 5-worker thread pool.

## How it functions

Lifecycle: built by `SonarrEpisodesManager` (non-critical) → init wires API/instance/cache refs → callers invoke individual analytics methods on demand. Internally everything funnels through `get_episode_file_metadata` / `get_episode_files_for_series`, then the format/codec helpers aggregate those raw file dicts in plain Python loops.

No machine_learning brain module is invoked — the "recommend" / "suggest" / "detect" methods here are simple heuristics computed locally, not brain decisions.

## Criteria & examples

- **Codec drift:** `detect_codec_drift_by_season` keeps a season only if it has `>1` codec. Example: Season 2 has 8 files `x264` + 2 files `x265` → returned as `{2: {"x264": 8, "x265": 2}}`. A season that is uniformly `x265` is omitted.
- **Standardization suggestion:** if `get_codec_summary` yields `{("WEBDL-1080p","x264","aac"): 40, ("Bluray-1080p","x265","ac3"): 12}`, then `suggest_codec_standardization` returns `{"suggested_format": ("WEBDL-1080p","x264","aac"), "count": 40}`.
- **Deletion candidates:** `recommend_episode_deletion_candidates(inst)` with default `min_quality="SD"` returns exactly the files whose quality name equals `"SD"`; a `"WEBDL-720p"` file is not a candidate.
- **Orphan detection:** a file with `episodeId=9999` where no episode object has `id==9999` is returned as orphaned.

## In plain English

This is the archive's file inspector. It walks the shelves and answers questions like: "How big is this episode's video file?", "What formats do we have, and which is the most common one we should standardize on?", "Is one season a messy mix of two different video codecs?", and "Are there leftover files lying around that don't belong to any episode we track?" Think of cataloguing every Blu-ray and stream of *The Office*: it tallies that most episodes are 1080p x264 and flags the odd season that's half a different format. It only looks and reports — it never throws anything away (that's the deletion manager's job).

## Interactions

- **Parent:** `SonarrEpisodesManager`.
- **Siblings:** retrieval, history, monitoring, sharding, deletion.
- **Talks to:** `sonarr_api` (`_make_request`, `get_all_sonarr_apis`) and `instance_manager` (`resolve_instance`).
- **Brain modules:** none.
