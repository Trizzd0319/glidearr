# SonarrEpisodesRetrievalSyncManager

**File** — `scripts/managers/services/sonarr/episodes/retrieval/sync.py`
**One-liner** — Tracks per-instance episode sync state: stores/reads last-sync timestamps and MD5 fingerprints of core episode fields so the app can detect which episodes drifted (changed) since the previous run.

## What it does (for a senior Python engineer)

A CACHE-state submanager. It maintains two kinds of state per Sonarr instance — a last-sync timestamp (in `global_cache`) and a map of episode-id → content fingerprint (in `sonarr_cache`) — and provides a drift diff between the live episode list and the stored fingerprints.

Public methods:
- `get_last_sync_timestamp(instance_name)` — reads `global_cache.get("sonarr/<instance>/episodes/last_sync", {})["timestamp"]`.
- `update_last_sync_timestamp(instance_name)` — writes `{"timestamp": <utc isoformat now>}` to that key.
- `reset_all_sync_timestamps()` — deletes the `last_sync` key for every instance in `config.get_sonarr_instances()`.
- `generate_episode_fingerprint(episode) -> str` — MD5 of a JSON dump (sorted keys) of seven core fields: `seriesId`, `seasonNumber`, `episodeNumber`, `title`, `airDateUtc`, `hasFile`, `monitored`.
- `get_cached_episode_fingerprints(instance_name) -> dict` — reads `sonarr_cache.get("sonarr/<instance>/episodes/fingerprints")` (or `{}`).
- `set_cached_episode_fingerprints(instance_name, fingerprint_map)` — writes that map.
- `detect_episode_drift(instance_name, episodes) -> list` — returns episodes whose freshly-computed fingerprint differs from the cached one (keyed by `str(ep["id"])`); episodes without an `id` are skipped.
- `update_episode_fingerprints(instance_name, episodes)` — recomputes and stores `{str(id): fingerprint}` for all episodes that have an `id`.

External API endpoints touched: none — it operates purely on caches and the episode dicts it is handed.

FETCH / CACHE / APPLY: CACHE only (read/write of sync state). No Sonarr APPLY.

Config keys read: `config.get_sonarr_instances()` (instance enumeration in `reset_all_sync_timestamps`).
global_cache keys: `sonarr/<instance_name>/episodes/last_sync` (read/write/delete).
sonarr_cache keys: `sonarr/<instance_name>/episodes/fingerprints` (read/write).

dry_run: not referenced — note `update_last_sync_timestamp` / `update_episode_fingerprints` always write even under dry-run if called; callers must gate them.

Concurrency: `BaseManager` singleton; timestamps use timezone-aware UTC (`datetime.now(timezone.utc)`).

## How it functions

`__init__` resolves `self.manager`, `global_cache`, and `sonarr_cache` (from `kwargs["cache_manager"]` or the parent). The fingerprint is the heart of the design: by hashing only seven semantically-meaningful fields (sorted-key JSON → MD5), cosmetic/volatile fields are ignored, so `detect_episode_drift` flags an episode only when something the app cares about (a new file, a monitor toggle, a retitle, a schedule change) actually changes. The typical cycle is: pull live episodes elsewhere → `detect_episode_drift` to find changes → process them → `update_episode_fingerprints` + `update_last_sync_timestamp` to record the new baseline.

No decision is delegated to a `machine_learning` brain module.

## Criteria & examples

- **Drift rule:** an episode drifts iff `cached.get(str(id)) != generate_episode_fingerprint(ep)`. Worked example: episode id 4821 was last cached with `hasFile=False`; tonight Sonarr reports `hasFile=True` (file imported). The seven-field hash changes, so 4821 lands in the `drifted` list and gets reprocessed; an episode whose fields are byte-identical to last run produces the same MD5 and is skipped.
- **Fields that do/don't trigger drift:** changing `title` from "TBA" to "The Wire Tap" drifts; a change to a field *not* in the seven (e.g. `overview` or `images`) does **not** drift.
- **Reset:** `reset_all_sync_timestamps()` forces the next run to treat every episode as new (because the timestamp is gone), logging `🗑️ Reset last sync timestamp for <instance>` per instance.

## In plain English

Imagine taking a photo of your DVR's episode list and stashing it in a drawer. Tonight you take a fresh photo and compare: which episodes look different from last time? Maybe a Severance episode that was "scheduled" now shows "recorded," or one got renamed. This manager does exactly that — it keeps a tiny fingerprint (a checksum) of the important details of each episode, and on the next run it spots only the ones that genuinely changed, so the app doesn't waste effort re-processing thousands of unchanged episodes. It also notes the time it last checked, and can wipe those notes to force a full re-scan.

## Interactions

- **Parent manager:** `SonarrEpisodesRetrieval` (`SonarrEpisodesRetrievalManager`).
- **Siblings:** `fetch`, `enrich`, `tvdb`, `validate`, `episode_cache`.
- **Services it talks to:** `global_cache` (last-sync timestamps), `sonarr_cache` (fingerprint maps), `config` (instance enumeration). No external HTTP.
- **Brain modules:** none.
