# SonarrEpisodesMonitoringManager

- **File** — `scripts/managers/services/sonarr/episodes/monitoring.py`
- **One-liner** — Flips Sonarr's per-episode `monitored` flag based on download/cutoff state: unmonitors episodes that are downloaded and quality-complete, re-monitors ones whose cutoff is unmet — always protecting pilots that aren't replicated elsewhere.

## What it does (for a senior Python engineer)

`SonarrEpisodesMonitoringManager(BaseManager, ComponentManagerMixin)` (`parent_name = "SonarrEpisodesMonitoringManager"`) is the monitoring-toggle child of `SonarrEpisodesManager`. It is an **APPLY** adapter — it issues Sonarr episode-monitoring updates (bulk and single) — preceded by **FETCH** of the episode list.

`__init__` resolves `self.manager`, `self.sonarr_api`, `self.dry_run`, and the dual cache (`global_cache` + `sonarr_cache`).

Public methods:

- `batch_unmonitor_downloaded_if_cutoff_met(instance)` — fetch `get_episodes(instance)`; collect episodes that are `monitored AND hasFile AND cutoffMet`, excluding any with a tag in `config.get("never_unmonitor_tags", [])`, and excluding non-replicated pilots (S01E01 where `_is_pilot_available_elsewhere` is `False`). Pushes a single `bulk_update_episodes([{id, monitored: False}, ...])`. Logs how many were unmonitored, or "none met cutoff."
- `batch_monitor_cutoff_unmet(instance)` — fetch episodes; for those currently **unmonitored** whose `cutoffMet` is falsy (`not episode.get("cutoffMet", True)`), bulk-set `monitored: True`.
- `auto_unmonitor_downloaded(instance)` — same selection as the batch-unmonitor variant, but applies **per-episode** via `update_episode_monitoring(instance, id, monitored=False)` (with the tag exemption + pilot guard, logged individually).
- `monitor_episodes_with_unmet_cutoff(instance)` — per-episode re-monitor of unmonitored episodes with unmet cutoff via `update_episode_monitoring(..., monitored=True)`; counts and logs the total re-monitored.
- `_is_pilot_available_elsewhere(current_instance, episode)` — internal pilot guard: iterates `config.get_sonarr_instances()` (excluding current), fetches `get_episodes(name)`, returns `True` if the same (series, season, episode) exists *with a file* elsewhere.

- Position in the tree: parent `SonarrEpisodesManager`; loads no submanagers.
- FETCH: `get_episodes`. CACHE: none. APPLY: `bulk_update_episodes`, `update_episode_monitoring`.
- API endpoints: via `sonarr_api.get_episodes`, `sonarr_api.bulk_update_episodes`, `sonarr_api.update_episode_monitoring`.
- Config keys: `never_unmonitor_tags` (list, default `[]`); `config.get_sonarr_instances()` (pilot guard).
- global_cache / Parquet keys: none read/written.
- **dry_run note:** `self.dry_run` is stored but **not consulted** in any of the monitoring methods — the bulk/single update calls fire regardless of `dry_run`. (Worth flagging: unlike the deletion manager, these APPLY calls are not gated behind a `dry_run` check in this file.)
- Concurrency: none.

## How it functions

Lifecycle: built by `SonarrEpisodesManager` (non-critical) → init wires API/cache refs → callers invoke a monitor/unmonitor policy. Each method fetches the episode list once, filters by `monitored`/`hasFile`/`cutoffMet`/tags/pilot-guard, then either accumulates a bulk payload (the `batch_*` methods) or updates episode-by-episode (the non-batch methods).

No machine_learning brain module is invoked — the keep/drop monitoring decision here is rule-based on Sonarr's own `cutoffMet`/`hasFile`/tag fields. (The codec/quality value-judgements that feed `cutoffMet` come from Sonarr's quality profile, not this manager.)

## Criteria & examples

- **Unmonitor rule:** an episode with `monitored=True, hasFile=True, cutoffMet=True`, no `never_unmonitor` tag, and not a pilot → added to the unmonitor batch. Example: S02E05 of *Andor* downloaded at its profile cutoff is unmonitored so Sonarr stops searching for upgrades.
- **Tag exemption:** if `never_unmonitor_tags = ["keep"]` and the episode carries tag `"keep"`, it is skipped (stays monitored).
- **Pilot guard:** S01E01 of *Bluey* is downloaded and cutoff-met, but no other instance holds that pilot with a file → `_is_pilot_available_elsewhere` returns `False` → it is **not** unmonitored (Sonarr keeps watching it).
- **Re-monitor rule:** an episode with `monitored=False` and `cutoffMet` falsy (e.g. the file was manually deleted or a better quality is now wanted) → re-set to `monitored=True` so Sonarr searches again.

## In plain English

Sonarr keeps a "watch list" of episodes it actively hunts for. This manager grooms that list. Once an episode is downloaded at the quality you asked for, it crosses it off the watch list (un-monitors it) so Sonarr stops wasting effort re-searching — like ticking off a *Stranger Things* episode you've already got in the best version you wanted. If an episode goes missing or you decide you want a higher quality, it puts it back on the list. The one thing it always protects: the pilot episode of a show — it won't stop watching for a series' first episode unless an identical copy is safely held on another server.

## Interactions

- **Parent:** `SonarrEpisodesManager`.
- **Siblings:** retrieval, file, history, sharding, deletion.
- **Talks to:** `sonarr_api` (`get_episodes`, `bulk_update_episodes`, `update_episode_monitoring`), `config` (`never_unmonitor_tags`, `get_sonarr_instances`).
- **Brain modules:** none.
