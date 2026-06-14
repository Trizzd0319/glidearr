# SonarrMonitoringPriorityQueueManager

- **File** — `scripts/managers/services/sonarr/monitoring/priority_queue.py`
- **One-liner** — Scores every Sonarr series with a hand-tuned heuristic (Tautulli watch signals, tags, storage pressure, completeness) into a ranked "keep watching / stop watching" queue, then optionally applies the monitored/unmonitored decision.

## What it does (for a senior Python engineer)

`SonarrMonitoringPriorityQueueManager(BaseManager, ComponentManagerMixin)`, `parent_name = "SonarrMonitoring"`. Resolves parent from `manager` kwarg/registry; pulls `sonarr_api`, `sonarr_cache`, `dry_run`, and also `self.tautulli = registry.get("manager", "TautulliManager")` (may be `None`).

It performs **FETCH** (live `sonarr_api.get_series(instance)`), **CACHE** (reads several cached signal dicts), and **APPLY** (`update_series_monitoring`). It loads no submanagers.

Public methods:
- `get_threshold_for_instance(instance, default=10) -> int` — returns `config["sonarr_thresholds"][instance]["default"]` (note: the **`default`** sub-key, not `critical`/`warning`), falling back to `10`.
- `classify_severity(percent_free) -> str` — hardcoded `critical (<5) / warning (<10) / ok` (does not read config; duplicates the space-thresholds logic with fixed numbers).
- `compare_with_previous(instance_name, current_summary) -> list` — reads cached `sonarr/storage/summary` (per instance), matches entries by `path`, annotates each with `deltaPercent`, and returns `(path, delta)` tuples where free space **dropped** (delta < 0). (Helper; not called by the run path in this file.)
- `build_priority_queue(instance, include_unmonitored=True) -> list` — the scorer. Returns a list of `{id, title, score, monitored, severity}` sorted by `score` descending.
- `apply_monitoring_priority(instance, queue, dry_run=False)` — for each entry, computes `target_status = (score >= 2)` and, if it differs from the current `monitored` state, either logs a "would change" line (dry_run) or calls `sonarr_api.update_series_monitoring(sid, monitored=target_status)`.
- `run_across_all_instances(include_unmonitored=True)` — for every instance in `get_all_sonarr_apis()`, build the queue then apply it.

**Config keys read:** `sonarr_thresholds[instance]["default"]`.
**Cache keys read:** `sonarr/<instance>/sync/tautulli_viewed`, `sonarr/<instance>/sync/tautulli_rewatches`, `sonarr/<instance>/monitoring/monitoredSeries`, `sonarr/<instance>/monitoring/unmonitoredSeries`, `sonarr/<instance>/storage/free_percent`, and (in `compare_with_previous`) `sonarr/storage/summary`.
**API touched:** `sonarr_api.get_series(instance)`, `sonarr_api.get_all_sonarr_apis()`, `sonarr_api.update_series_monitoring(...)`.
**dry_run:** honored in `apply_monitoring_priority` (`dry_run = dry_run or self.dry_run`); when set, mutations become "[Dry Run] Would change monitoring for <title> → <target>" log lines and nothing is written.

Note: the monitored/unmonitored sets are read from `sonarr/<instance>/monitoring/monitoredSeries`, but the series/backfill managers write the equivalent data to the `sonarr/<instance>/sync/monitored` key (`CacheKeyPaths.sonarr.MONITORED_SYNC`) under nested `monitoredSeries`/`unmonitoredSeries` fields. These are different cache paths, so unless something else populates the `monitoring/...` keys, `monitored_ids`/`unmonitored_ids` here may come back empty — worth verifying against the live cache.

## How it functions

Lifecycle: standard `__init__` → `register()` → resolve deps (incl. optional Tautulli). The run path is `run_across_all_instances()` → per instance `build_priority_queue()` → `apply_monitoring_priority()`.

`build_priority_queue` gathers signals: if Tautulli is present it reads cached `tautulli_viewed` titles into a `watched_titles` set and the `tautulli_rewatches` map; it loads the monitored/unmonitored id sets and `free_percent`; then it fetches the live series list and scores each one. The decision (`score >= 2` ⇒ monitor) is computed inline; no `machine_learning` brain module is consulted — this is a self-contained heuristic.

## Criteria & examples

Per-series score additions/subtractions (starting from 0):
- title in Tautulli `watched_titles`: **+2**
- title in Tautulli `rewatches`: **+2**
- unmonitored AND `include_unmonitored`: **+1**
- tag `keep`: **−5**; tag `archive`: **−3**; tag `active`: **+1**
- missing `tvdbId` or missing `images`: **−1**
- `added` string contains `"2024"`: **+1**
- `episodeFileCount == 0` but `episodeCount > 0` (has episodes, no files): **−3**
- `free_percent` below the instance's `default` threshold: **−2**

Apply rule: `target_status = score >= 2`.

Worked example — a show watched recently in Tautulli (+2) and tagged `active` (+1) scores **3 ≥ 2 → monitored**. A show tagged `keep` (−5) plus a rewatch (+2) nets **−3 < 2 → would be unmonitored**, except `keep` is the explicit "never touch" tag — here the heuristic would still mark it for unmonitoring, which is why the *rules/scheduler/episodes* managers separately short-circuit `keep`-tagged series via `tag_monitor.is_series_tagged_keep`; this priority-queue scorer does **not** consult that guard, so it relies on the −5 weight alone to keep `keep` shows below the +2 monitor line. Another example: a 2024-added show with episodes but zero files on a near-full disk scores `+1 (2024) −3 (no files) −2 (low space) = −4 < 2 → unmonitored`.

## In plain English

This is a "what's worth keeping on the DVR" ranker. It gives each show points for being watched or re-watched recently, a little bonus if it's freshly added or marked "active," and takes points away if you flagged it "keep storage low/archive," if it looks broken (no artwork, no actual video files), or if the drive is nearly full. Anything that ends up with at least 2 points stays in active recording; the rest get switched off. So a binge-watched *Stranger Things* climbs to the top and keeps recording, while a half-broken, never-watched show on a stuffed drive drops off the list.

## Interactions

- **Parent:** `SonarrMonitoring` (`SonarrMonitoringManager`).
- **Siblings:** shares storage-pressure logic conceptually with `SonarrMonitoringSpaceThresholdsManager`; complements `SonarrMonitoringSeriesManager`/`Backfill` which produce the monitored/unmonitored caches.
- **Services:** Sonarr API (series fetch + monitoring writes), Sonarr cache (signal reads), and **TautulliManager** (watch/rewatch signals, looked up by name in the registry; optional).
- **Brain modules:** none — scoring is local heuristic, not delegated to `machine_learning`.
