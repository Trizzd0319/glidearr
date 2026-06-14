# SonarrEpisodesHistoryManager

- **File** — `scripts/managers/services/sonarr/episodes/history.py`
- **One-liner** — Queries Sonarr's `/history` API to surface recent episode activity: recent import/rename events, the set of series with updates in the last N days, and per-episode "watch" (history event) counts.

## What it does (for a senior Python engineer)

`SonarrEpisodesHistoryManager(BaseManager, ComponentManagerMixin)` (`parent_name = "SonarrEpisodes"`) is a **FETCH**-only child of `SonarrEpisodesManager`. Notably, it bypasses `sonarr_api` and makes **raw `requests.get`** calls directly against each instance's Sonarr REST API, reading `base_url` + `api` key out of config.

`__init__` is ordered slightly differently from its siblings: it resolves `self.manager`, `dry_run`, caches, and `sonarr_api` **before** calling `super().__init__(...)` (so the base init receives the resolved `global_cache`), then `self.register()`.

Public methods (all `@LoggerManager().log_function_entry`-decorated):

- `get_recent_episode_events(instance_name, since_timestamp, event_types=None)` — GET `{base_url}/api/v3/history/since?date={ts}&includeSeries=false&includeEpisode=true`. Returns the list of `item["episode"]` objects whose `eventType` is in the allowed set. Default allowed events: `{"downloadFolderImported", "seriesFolderImported", "episodeFileRenamed"}`. Returns `[]` on missing config or HTTP error.
- `get_series_with_recent_history(instance_name, days_back=2, page_size=1000, max_retries=5)` — paginates GET `{base_url}/api/v3/history` (descending, `date=now-days_back`), accumulating the set of `seriesId` values whose entry `date > since`. Handles HTTP 429 with **exponential backoff** (`wait = 2 ** retries`, up to `max_retries`); stops when a page returns fewer than `page_size` rows. Returns a `set` of series IDs.
- `build_episode_watch_counts(instance_name, days_back=30)` — paginates GET `{base_url}/api/v3/history` (pageSize 1000, descending, `date=now-days_back`) and tallies `{episodeId: count}` across all history events in the window. Returns the dict.

- Position in the tree: parent `SonarrEpisodesManager`; loads no submanagers.
- FETCH: direct `requests.get` against `/api/v3/history`, `/api/v3/history/since`. CACHE: none. APPLY: none.
- API endpoints: `GET /api/v3/history/since`, `GET /api/v3/history` (paginated).
- Config keys: `config.get("sonarr_instances", {})[instance_name]` → uses that instance's `base_url` and `api` (the API key, accessed as `instance_config['api']`).
- global_cache / Parquet keys: none read/written (results are returned to the caller, not persisted here).
- dry_run: stored but unused (read-only manager).
- Concurrency: none; rate-limit handling is sequential backoff via `time.sleep(2 ** retries)`.

## How it functions

Lifecycle: built by `SonarrEpisodesManager` (non-critical) → init resolves deps (and passes the resolved cache up to `super().__init__`) → callers invoke a history query. Each method looks up the instance's config, constructs the `/history` URL with an `X-Api-Key` header, and either does a single GET (`get_recent_episode_events`) or loops pages until exhaustion (`get_series_with_recent_history`, `build_episode_watch_counts`).

Note the term "watch counts" here means **Sonarr history-event counts per episodeId** (imports/renames/etc.), not Tautulli/Plex playback. It is a measure of activity in Sonarr's history, not viewer watches.

No machine_learning brain module is invoked.

## Criteria & examples

- **Event filter:** `get_recent_episode_events(..., event_types=None)` keeps only items whose `eventType` ∈ `{downloadFolderImported, seriesFolderImported, episodeFileRenamed}`. An entry with `eventType="grabbed"` is dropped; a `downloadFolderImported` entry with a populated `episode` object is kept.
- **429 backoff:** on the 1st 429 it waits `2**0 = 1s`, then `2s`, `4s`, `8s`, `16s` up to `max_retries=5`; if still failing it returns the partial `updated_series_ids` set.
- **Recency window:** `get_series_with_recent_history(days_back=2)` collects series whose history entry `date` is newer than `now-2d`. A series last touched 5 days ago contributes nothing.
- **Watch tally:** if episode `12345` appears in 3 history events within the last 30 days, `build_episode_watch_counts(...)` yields `{12345: 3, ...}`.

## In plain English

This is the archive's logbook reader. It flips through Sonarr's recent activity log and answers questions like: "Which episodes just got downloaded or re-filed?", "Which shows have had any activity in the last couple of days?", and "How busy has each episode been in the log lately?" For example, if *The Last of Us* just imported four new episodes overnight, this reader spots that the show had recent activity and lists the episodes involved — useful for only re-processing things that actually changed, instead of re-scanning the entire library every time.

## Interactions

- **Parent:** `SonarrEpisodesManager`.
- **Siblings:** retrieval, file, monitoring, sharding, deletion.
- **Talks to:** Sonarr's REST `/history` API directly via the `requests` library (using `base_url` + `api` key from `config["sonarr_instances"]`). Does not route through `sonarr_api`.
- **Brain modules:** none.
