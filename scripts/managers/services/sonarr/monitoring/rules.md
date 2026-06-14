# SonarrMonitoringRulesManager

- **File** — `scripts/managers/services/sonarr/monitoring/rules.py`
- **One-liner** — Declarative monitoring policy engine: applies a fixed set of rules (popularity, ratings, awards, ended-and-unwatched, user genre preferences, global overrides) to decide whether each series should be monitored, then applies that via the Sonarr API.

## What it does (for a senior Python engineer)

`SonarrMonitoringRulesManager(BaseManager, ComponentManagerMixin)`. Like `series.py` it derives `parent_name` dynamically from its class name (`"SonarrMonitoringRulesManager"` → `"SonarrMonitoringRules"`). Resolves parent from `manager` kwarg/registry; pulls `sonarr_api`, `logger`, `manager`, `dry_run`, and `self.tag_monitor = self.get_tag_monitor()` (keep-tag guard). Raises `ValueError` if no logger. Loads no submanagers.

It is **FETCH** (series reads) + **APPLY** (`update_series_monitoring` / `update_episode_monitoring`). No cache reads/writes.

Public methods (rule application):
- `apply_monitoring_rules(series_data)` — for each series: skip if `keep`-tagged; ensure the pilot is monitored; then `should_auto_monitor` → monitor, `elif should_auto_unmonitor` → unmonitor; then independently monitor if `should_monitor_due_to_awards` or `should_monitor_due_to_high_rating`. (Note: this main path does **not** check `self.dry_run` — it issues the PUTs directly.)
- `ensure_pilot_episode_monitored(series)` — finds S01E01 and, if unmonitored, monitors it via `update_episode_monitoring`.
- `evaluate_series_priority(series_list)` — sorts by `(watchCount, popularityScore)` descending (pure, no side effects).
- `auto_apply_user_rules(user_preferences)` — monitors series whose `genre` is in `preferred_genres`; unmonitors series whose `title` is in `excluded_titles`. **Honors `dry_run`** (logs "[DRY-RUN] Would ..." instead).
- `prioritize_frequently_watched_series(watched_series)` — monitors series with `watchCount > 10`. Honors `dry_run`.
- `enforce_global_monitoring_settings()` — reads `config["global_monitoring_settings"]`; if `force_monitor_all` monitor everything; if `force_unmonitor_archived` unmonitor series with `status == "archived"`. Honors `dry_run`.

Rule predicates (pure):
- `should_auto_monitor(series)` → `popularityScore > 80`.
- `should_auto_unmonitor(series)` → `status == "ended"` AND `watchCount == 0`.
- `should_monitor_due_to_awards(series)` → `"Emmy"` or `"Golden Globe"` in `series["awards"]`.
- `should_monitor_due_to_high_rating(series)` → `rating >= 8.5`.

**API touched:** `update_series_monitoring`, `update_episode_monitoring`, `get_series_episodes`, `get_all_series`.
**Config keys read:** `preferred_genres`/`excluded_titles` (passed in as `user_preferences`, not config-loaded here), `global_monitoring_settings` (`force_monitor_all`, `force_unmonitor_archived`).
**Cache keys:** none.
**dry_run:** honored by `auto_apply_user_rules`, `prioritize_frequently_watched_series`, and `enforce_global_monitoring_settings`; **not** honored by `apply_monitoring_rules`/`ensure_pilot_episode_monitored` (those PUT unconditionally — flag this inconsistency).

These rules are local heuristics living in the *manager*; no decision is delegated to a `machine_learning` brain module. (Contrast the design intent that value-judgements move into `machine_learning/` — this file predates/sidesteps that and judges inline.)

## How it functions

Lifecycle: `__init__` (dynamic `parent_name`, logger guard, `tag_monitor`) → `register()`. There's no single `run()`; each rule family is its own public method a caller invokes. `apply_monitoring_rules` is the closest thing to a main loop: keep-tag guard → pilot guarantee → monitor/unmonitor decision → award/rating overrides (which can re-monitor something the prior step unmonitored). The predicates are trivially testable pure functions reading series-dict fields.

## Criteria & examples

- **Popularity:** a series with `popularityScore = 92` → `> 80` → auto-monitored. `popularityScore = 75` → not auto-monitored by this rule.
- **Ended & unwatched:** `status="ended", watchCount=0` → auto-unmonitored. `status="ended", watchCount=3` → left alone (someone watched it).
- **Awards override:** a series with `awards = ["Emmy"]` is monitored even if it would otherwise be unmonitored — e.g. *Succession* (ended, watchCount 0) gets unmonitored by the ended-rule but then re-monitored by the awards rule.
- **Rating override:** `rating = 8.7` → `>= 8.5` → monitored; `rating = 8.4` → not.
- **High watch count:** `watchCount = 11` → `> 10` → monitored by `prioritize_frequently_watched_series`; `watchCount = 10` is **not** (strict `>`).
- **Global force:** with `global_monitoring_settings = {"force_unmonitor_archived": true}`, every series with `status == "archived"` is unmonitored.
- **Pilot guarantee:** S01E01 of any processed series is forced monitored if it wasn't (so the show can re-acquire its first episode).

## In plain English

This is the rulebook for "what's worth recording." It auto-keeps shows that are popular (over 80 popularity), highly rated (8.5+), or award-winning (Emmy/Golden Globe). It auto-drops shows that have ended and that nobody ever watched. It always makes sure the very first episode (the pilot) of a show is set to record, so you can give a series a chance. It can also follow your personal preferences — record shows in your favorite genres, never record titles on your blocklist — and respect house-wide switches like "record everything" or "stop recording anything archived." A handy mental picture: even if *Game of Thrones* finished and you somehow never watched it (so the "ended & unwatched" rule would drop it), its pile of Emmys flips it right back to "keep recording."

## Interactions

- **Parent:** `SonarrMonitoring` (registry name `SonarrMonitoringRules`).
- **Managers it talks to:** `SonarrSyncTagsManager` via `get_tag_monitor()` for the keep-tag skip.
- **Services:** Sonarr API (series + episode monitoring writes, episode fetches).
- **Brain modules:** none — judgements are inline predicates, not delegated to `machine_learning`.
