# RadarrMonitoringRulesManager

**File** — `scripts/managers/services/radarr/monitoring/rules.py`
**One-liner** — Applies tag-aware and public-rating-based monitoring rules to Radarr movies, auto-monitoring strong titles while honoring "keep" tags and dry_run.

## What it does (for a senior Python engineer)

`RadarrMonitoringRulesManager(BaseManager, ComponentManagerMixin)` is a child of `RadarrMonitoringManager` (declares `parent_name = "RadarrMonitoringManager"`). It is the decision-and-APPLY layer for rating/tag-based monitoring. It loads no submanagers.

**Shared deps:** `radarr_api`, `instance_manager`, `dry_run` from `kwargs` or the parent (`kwargs["manager"]`). Instances pass through `_resolve_instance` (instance_manager → radarr_api → literal/`"default"`).

**Key public methods:**

- `apply_monitoring_rules(movie_data: list, instance: str)` — the main APPLY loop. For each movie: skip if any of its tags maps to a label in `never_unmonitor_tags`; skip if already `monitored`; otherwise if `should_monitor_due_to_strong_ratings(movie)` is True, set `monitored=True` and `PUT movie/{id}` with the full movie payload. Honors `dry_run` by logging `"[dry_run] Would auto-monitor ..."` and mutating nothing.
- `should_monitor_due_to_strong_ratings(movie) -> bool` — pure predicate: True if any rating source has `votes >= 1000` and `value >= 8.0`.
- `should_auto_unmonitor(movie, instance) -> bool` — pure predicate: True if any rating source has `votes >= 500` and `value <= 4.0`, OR a tag label contains a blacklist keyword (`lowest`/`worst`/`bottom`/`avoid`/`trash`). (This predicate only *evaluates*; this file never calls it itself.)

**FETCH / CACHE / APPLY:** APPLY (`PUT movie/{id}`). It reads no movie list itself — `apply_monitoring_rules` is fed `movie_data` by the caller. No CACHE writes.
**API endpoints:** `movie/{id}` (PUT only).
**Config keys:** `never_unmonitor_tags` (list of tag labels treated as "keep"; note the rule lowercases tag labels and tests `tag_names & never_monitor_tags`, so config values are expected to be label strings here).
**global_cache keys read:** `radarr.tags.{resolved}` and `radarr.tags.{instance}` (to resolve tag ids → labels).
**dry_run:** honored — when `self.dry_run` is True the auto-monitor PUT is skipped and a "would" line is logged.
**Singleton/concurrency:** standard `BaseManager` singleton; no threading.

## How it functions

Lifecycle: `__init__` → `super().__init__` → `self.register()` → resolve `radarr_api`/`instance_manager`/`dry_run` → debug log. No `load_components`.

`apply_monitoring_rules` resolves the instance, loads `never_unmonitor_tags` into a set, loads the instance's tag labels from `global_cache`, then iterates the supplied `movie_data`. For each movie it intersects the movie's resolved tag-name set with the keep set, short-circuits on already-monitored, and gates the PUT on the strong-ratings predicate. Each PUT is wrapped in try/except so one failure does not abort the loop.

No decision is delegated to a `machine_learning` brain module — the monitoring criteria are inline thresholds in this file.

## Criteria & examples

- **Keep-tag skip** — config `never_unmonitor_tags = ["keep"]`; a movie tagged with the id mapping to label `"Keep"` is skipped (label lowercased to `keep`, which is in the set), regardless of ratings.
- **Already-monitored skip** — a movie with `monitored=True` is skipped.
- **Auto-monitor threshold** — an unwatched, untagged, unmonitored movie with IMDb `votes = 1200, value = 8.1` is auto-monitored (1200 ≥ 1000 and 8.1 ≥ 8.0). The same movie with `value = 7.5` is left alone (7.5 < 8.0). A cult favorite with `value = 9.0` but only `votes = 400` is left alone (400 < 1000 votes — not enough public consensus).
- **dry_run** — with `dry_run=True`, the qualifying 8.1-rated movie logs `[dry_run] Would auto-monitor '<title>'` and is not PUT.
- **Auto-unmonitor predicate** — `should_auto_unmonitor` returns True for a movie rated `value = 3.0` with `votes = 600` (600 ≥ 500, 3.0 ≤ 4.0), or for one tagged `"trash-pile"` (matches blacklist keyword `trash`).

## In plain English

This is the bouncer with a clipboard deciding which movies get a "keep watching for a better copy" sticker. If a movie is a clear crowd-pleaser — lots of people rated it and the score is high, like a beloved Marvel film with thousands of strong reviews — it gets the sticker automatically. If you've already hand-flagged a movie as "keep," the bouncer leaves it alone. And if the system is in "practice mode" (dry_run), the bouncer just announces what it *would* do without actually touching anything.

## Interactions

- **Parent:** `RadarrMonitoringManager`.
- **Siblings:** `RadarrMonitoringMoviesManager` (shares the same rating/tag heuristics and owns bulk/batch toggles), `RadarrMonitoringSchedulerManager`, `RadarrMonitoringHistoryManager`.
- **Services:** Radarr API (`PUT movie/{id}`), instance manager (`resolve_instance`), `global_cache` (tag labels).
- **Brain modules:** none.
