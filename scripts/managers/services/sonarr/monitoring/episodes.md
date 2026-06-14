# SonarrMonitoringEpisodesManager

- **File** — `scripts/managers/services/sonarr/monitoring/episodes.py`
- **One-liner** — Episode-level monitoring control: monitors upcoming episodes, batch-updates with rollback, auto-unmonitors already-watched episodes, and keeps "specials"/holiday episodes monitored or not per policy.

## What it does (for a senior Python engineer)

`SonarrMonitoringEpisodesManager(BaseManager, ComponentManagerMixin)`, `parent_name = "SonarrEpisodesMonitoring"`. Resolves parent from `manager` kwarg/registry; pulls `sonarr_api`, `logger`, `sonarr_cache`, `dry_run`, and `self.tag_monitor = self.get_tag_monitor()` — the shared keep-tag monitor (`SonarrSyncTagsManager`, resolved/created via `BaseManager.get_tag_monitor`) exposing `is_series_tagged_keep(series_id)`. Raises `ValueError` if no logger. Loads no submanagers.

It is mostly **FETCH** + **APPLY** at the episode granularity (the per-episode `monitored` PUT). No cache writes of its own.

Public methods:
- `get_all_episode_monitoring(instance)` — `sonarr_api.get_all_episodes(instance)` (FETCH passthrough).
- `update_monitoring_state(episode_id, instance, monitored)` — `sonarr_api.update_episode_monitoring(instance, episode_id, monitored)` (single APPLY).
- `monitor_upcoming_episodes(series_id, last_ep, season, user, instance)` — fetches `get_upcoming_episodes(series_id)` and monitors every episode in `season` whose `episodeNumber > last_ep`.
- `batch_monitor_episodes(episode_ids, instance)` — monitors each id; on any failure it logs an error and **rolls back** the successful ones to unmonitored (best-effort, each rollback in try/except).
- `track_monitored_episodes(series_id, instance) -> list` — returns the currently-monitored episodes of a series.
- `auto_unmonitor_watched_episodes(monitored_episodes, instance, series_id)` — short-circuits if the series is `keep`-tagged (`tag_monitor.is_series_tagged_keep`); otherwise unmonitors any episode that `hasFile` and whose tags don't intersect `config["never_unmonitor_tags"]`.
- `ensure_specials_are_unmonitored(series_id, instance)` — for season-0 ("specials") episodes: keep monitored if tagged with a `special_keep_tags` value or airing within ±7 days of a hardcoded holiday; otherwise unmonitor.

**API touched:** `get_all_episodes`, `update_episode_monitoring`, `get_upcoming_episodes`, `get_series_episodes`.
**Config keys read:** `never_unmonitor_tags` (default `[]`), `special_keep_tags` (default `["holiday", "finale"]`).
**Cache keys:** none read/written here.
**dry_run:** captured into `self.dry_run` but **not consulted** — every method that flips an episode's `monitored` state PUTs unconditionally. (Flag: another mutating path that does not honor dry_run.)

The keep-tag short-circuit is the only decision delegated outward, and it goes to a sibling *manager* (`SonarrSyncTagsManager`), not to a `machine_learning` brain module.

## How it functions

Lifecycle: standard `__init__` → `register()` → resolve deps incl. `tag_monitor`. There is no single `run()`; callers invoke a specific verb. The interesting internal logic is in `ensure_specials_are_unmonitored`: it parses each episode's `airDate` (`%Y-%m-%d`), and for the season's air year compares against a fixed `holiday_dates` table (Halloween 10/31, Christmas 12/25, New Year's 1/1, Valentine's 2/14, Easter 4/9, Thanksgiving 11/23, July 4th 7/4, Labor Day 9/4, Memorial Day 5/29, Veterans Day 11/11, MLK Day 1/16, Presidents' Day 2/20); an episode within 7 days of any of those is kept. `batch_monitor_episodes` implements an all-or-nothing-ish transaction with rollback on partial failure.

## Criteria & examples

- **Watched-unmonitor rule** (`auto_unmonitor_watched_episodes`): episode is unmonitored iff `hasFile == True` AND none of its tags are in `never_unmonitor_tags`, AND the series is not `keep`-tagged. Example: episode S02E05 has a downloaded file and tags `["filler"]`, `never_unmonitor_tags = ["finale"]`, series not keep-tagged → unmonitored. If the series *were* keep-tagged, the whole call returns early and nothing changes.
- **Specials rule** (`ensure_specials_are_unmonitored`, season 0 only): a special tagged `holiday` (in `special_keep_tags`) stays monitored; a special airing 2024-12-23 is within 7 days of Christmas (12/25) → kept monitored even if untagged; an untagged special airing 2024-06-15 (no nearby holiday) → unmonitored.
- **Upcoming rule** (`monitor_upcoming_episodes`): with `season=3, last_ep=4`, upcoming S03E05 and S03E06 are monitored, S03E04 and any S02 episodes are not.
- **Batch rollback**: monitoring `[101, 102, 103]` where 102 throws → 101 and 103 are rolled back to unmonitored, leaving none newly monitored.

## In plain English

If the series-level manager decides *which shows* to record, this one works inside a single show deciding *which individual episodes* to record. It switches on recording for episodes that haven't aired yet, turns off recording for episodes you've already downloaded and watched (so Sonarr stops re-grabbing them), and has special manners for "specials"/bonus episodes — it'll keep a Christmas special or a season finale, but drop ordinary one-off specials. It also protects shows you've flagged "keep" — those are never touched. And if it tries to switch on a batch of episodes and one fails, it undoes the whole batch so you don't end up half-done. Think of pruning the recording list for *Doctor Who* — keep the Christmas specials, drop the random behind-the-scenes shorts you already saw.

## Interactions

- **Parent:** `SonarrMonitoring` (registry name `SonarrEpisodesMonitoring`).
- **Siblings / managers it talks to:** `SonarrSyncTagsManager` via `get_tag_monitor()` for the `keep`-tag guard; complements series-level monitoring done by `SonarrMonitoringSeriesManager` / `SonarrMonitoringRulesManager`.
- **Services:** Sonarr API (episode fetches + per-episode monitoring PUTs).
- **Brain modules:** none.
