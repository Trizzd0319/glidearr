# SonarrEpisodesDeletionManager

- **File** — `scripts/managers/services/sonarr/episodes/deletion.py`
- **One-liner** — Deletes episode files from Sonarr — expired ones, low-disk-pressure ones, and duplicate/redundant copies — while guarding pilot episodes that aren't replicated on another instance; honours `dry_run`.

## What it does (for a senior Python engineer)

`SonarrEpisodesDeletionManager(BaseManager, ComponentManagerMixin)` (`parent_name = "SonarrEpisodes"`) is the **APPLY** (DELETE) child of `SonarrEpisodesManager`. It reads episode-file lists and issues `DELETE episodefile/{id}` calls under various policies, with a consistent pilot-protection guard.

`__init__` resolves `self.manager`, `self.sonarr_api`, `self.instance_manager`, dual cache (`global_cache` + `sonarr_cache`), and `self.dry_run` (from kwargs or parent).

Public / notable methods:

- `pilot_exists_in_other_instance(series_id, season, episode_num, skip_instance)` — the **pilot guard**. Iterates all `config.get_sonarr_instances()` except `skip_instance`, fetches `get_episodes(name)`, and returns `True` if the same (series, season, episode) exists *with a file* on another instance. Used to decide whether a pilot is safe to delete.
- `delete_expired_episodes(days=30)` — for every instance: GET `episodefile`, parse each file's `dateAdded` (ISO, split on `Z`), and if older than `now - days`, delete it. **Guard:** S01E01 pilots are only deleted if `pilot_exists_in_other_instance(...)` is `True` (otherwise `continue`/skip). Respects `dry_run` (logs `[DRY-RUN] Would delete ...`).
- `delete_old_episodes(min_gb=20)` — disk-pressure deletion. For each instance, read free space via `self.sonarr_cache.storage.get_free_space(resolved)`; if free space is unknown → warn + skip; if `free_gb >= min_gb` → skip (enough room). Otherwise sort files by `(seasonNumber, episodeNumber)` and delete in that order, applying the same pilot guard. Respects `dry_run`.
- `remove_duplicate_episodes(instance)` — groups files by `(seriesId, seasonNumber, episodeNumber)`; for any group with `>1` file, sorts by `quality.quality.id` descending and deletes all but the highest-quality one (`group[1:]`). Pilot guard applies to S01E01 groups. Respects `dry_run`.
- `remove_redundant_similar_files(instance)` — like the above but ranks duplicates with a heuristic `score(ep)` = `quality.quality.id` `+5 if audioCodec contains "dts"` `+3 if videoCodec contains "x265"`; keeps the top-scored file and deletes the rest. Pilot guard applies. Respects `dry_run`.
- `warm_cache(logger, cache, config=None)` — **static** method. Builds its own `SonarrInstanceManager` and `SonarrAPI`, then for each instance populates `global_cache` key `sonarr/<instance>/episodes/all` (`CacheKeyPaths.sonarr.ALL_EPISODES`) via `get_or_generate_cache(generator=GET episodefile, expiration_time=86400)`. This is the one **CACHE** path in the file (24-hour TTL).

- Position in the tree: parent `SonarrEpisodesManager`; loads no submanagers.
- FETCH: `episodefile` GETs and `get_episodes`. CACHE: `warm_cache` writes `ALL_EPISODES`. APPLY: `DELETE episodefile/{id}`.
- API endpoints (via `sonarr_api._make_request` / `get_episodes`): `episodefile` (GET), `episodefile/{id}` (DELETE), and per-instance `get_episodes`.
- Config keys: `config.get_sonarr_instances()` (pilot guard).
- global_cache keys: writes `sonarr/<instance>/episodes/all` (in `warm_cache`); reads free space through `sonarr_cache.storage.get_free_space`.
- dry_run: when true, all delete branches log a "would delete" / "Deleting ..." line and skip the actual `DELETE` (`if not self.dry_run: ...`).
- Concurrency: none (sequential loops; tqdm progress bars).

## How it functions

Lifecycle: built by `SonarrEpisodesManager` (non-critical) → init wires API/instance/cache refs → callers invoke a specific deletion policy. Each policy fetches the file list, applies its selection rule, runs the shared pilot guard for any S01E01, then deletes (or logs in dry_run). `warm_cache` is a standalone static utility that bootstraps its own deps and pre-populates the all-episodes cache.

No machine_learning brain module is invoked — selection rules here (age cutoff, quality-id ordering, the `score()` heuristic, disk threshold) are local heuristics. (Higher-level, brain-driven space/lifecycle deletion lives elsewhere in the system; this manager is the low-level Sonarr DELETE adapter.)

## Criteria & examples

- **Expiry cutoff:** with `days=30`, a file `dateAdded=2026-04-01` evaluated on 2026-06-10 is `< now-30d` → delete candidate. A file added 2026-06-01 is younger than the cutoff → kept.
- **Pilot guard:** S01E01 of *The Mandalorian* is past the cutoff, but `pilot_exists_in_other_instance` finds no replicated copy with a file on any other instance → it is **skipped** (kept). If a 4K instance also holds that pilot with a file, the guard returns `True` and the HD copy may be deleted.
- **Disk pressure:** `delete_old_episodes(min_gb=20)` on an instance reporting `free_gb=12.5` (< 20) → deletion proceeds lowest season/episode first. An instance with `free_gb=45.0` → skipped entirely.
- **Redundancy scoring:** two copies of S03E04 — copy A `qualityId=7, audio="dts", video="x264"` → `7+5+0=12`; copy B `qualityId=8, audio="aac", video="x265"` → `8+0+3=11`. `remove_redundant_similar_files` keeps copy A (12 > 11) and deletes copy B.

## In plain English

This is the archive's cleanup crew. It tosses out episodes that have sat untouched too long, frees up shelf space when the disk is nearly full, and gets rid of duplicate copies — always keeping the best version of each episode. It has one important rule: never throw away the *very first episode* of a show (the pilot) unless an identical copy is safely stored somewhere else — because losing a pilot means you can't easily start the series again. And when the system is in "dry run" mode, the crew just writes down what it *would* delete instead of actually deleting, so you can review before anything is lost.

## Interactions

- **Parent:** `SonarrEpisodesManager`.
- **Siblings:** retrieval, file, history, monitoring, sharding.
- **Talks to:** `sonarr_api` (`_make_request`, `get_episodes`, `get_all_sonarr_apis`), `instance_manager` (`get_all_instance_names`, `resolve_instance`), `sonarr_cache.storage.get_free_space`, `config.get_sonarr_instances`, and (in `warm_cache`) `SonarrInstanceManager` + `SonarrAPI` + `CacheKeyPaths`.
- **Brain modules:** none directly.
