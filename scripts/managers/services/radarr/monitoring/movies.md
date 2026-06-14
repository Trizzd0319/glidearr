# RadarrMonitoringMoviesManager

**File** — `scripts/managers/services/radarr/monitoring/movies.py`
**One-liner** — Movie-level monitoring control for Radarr: read movies, toggle/bulk/batch the `monitored` flag, and flip monitoring based on quality-cutoff state.

## What it does (for a senior Python engineer)

`RadarrMonitoringMoviesManager(BaseManager, ComponentManagerMixin)` is a child of `RadarrMonitoringManager` (declares `parent_name = "RadarrMonitoringManager"`). It is a thin Radarr adapter that both FETCHes movie state and APPLYs monitoring changes. It loads no submanagers of its own.

**Shared deps:** `radarr_api`, `instance_manager`, `dry_run` resolved from `kwargs` or the parent passed as `kwargs["manager"]`. All instance arguments first pass through `_resolve_instance(instance)`, which prefers `instance_manager.resolve_instance`, falls back to `radarr_api.resolve_instance`, then to the literal value or `"default"`.

**Key public methods:**

- `get_all_movies(instance) -> list` — FETCH `GET movie`, fallback `[]`.
- `get_monitored_movies(instance) -> list` / `get_unmonitored_movies(instance) -> list` — filter `get_all_movies` by the `monitored` flag.
- `update_movie_monitoring(movie_id, instance, monitored: bool) -> bool` — FETCH `GET movie/{id}`, mutate `monitored`, then APPLY `PUT movie/{id}`. Returns truthiness of the PUT response. (No dry_run guard — see note below.)
- `toggle_movie_monitoring(movie_id, instance, monitored: bool) -> bool` — same fetch-mutate-PUT as above, but with info/warning logging of the new state.
- `bulk_monitor_movies(movie_ids: list, instance, monitor=True) -> bool` — APPLY `PUT movie/editor` with payload `{"movieIds": [...], "monitored": <bool>}`. Returns `False` immediately on empty `movie_ids`.
- `batch_update_monitoring(instance, updates: dict)` — per-movie `toggle_movie_monitoring` for `{movie_id: state}`; on any failures it logs and **rolls back** every failed id to the inverse of the requested state (`not updates[mid]`).
- `batch_unmonitor_downloaded_if_cutoff_met(instance)` — unmonitor movies that are monitored, have a file, are not tagged in `never_unmonitor_tags`, and whose quality cutoff is met (`qualityCutoffNotMet is False` AND `hasFile`). Issues one `bulk_monitor_movies(..., monitor=False)`.
- `batch_monitor_cutoff_unmet(instance)` — re-monitor movies that are currently unmonitored and have `qualityCutoffNotMet == True`. Issues one `bulk_monitor_movies(..., monitor=True)`.
- `auto_unmonitor_downloaded(instance)` / `monitor_movies_with_unmet_cutoff(instance)` — convenience aliases for the two batch methods above.
- `should_auto_monitor(movie) -> bool` — pure predicate: True if any rating source has `votes >= 1000` and `value >= 8.0`.
- `should_auto_unmonitor(movie, instance) -> bool` — pure predicate: True if any rating source has `votes >= 500` and `value <= 4.0`, OR a tag label contains a blacklist keyword (`lowest`/`worst`/`bottom`/`avoid`/`trash`).

**FETCH / CACHE / APPLY:** FETCH (`GET movie`, `GET movie/{id}`) and APPLY (`PUT movie/{id}`, `PUT movie/editor`). No CACHE writes here.
**API endpoints:** `movie`, `movie/{id}`, `movie/editor`.
**Config keys:** `never_unmonitor_tags` (list of tag ids treated as "keep").
**global_cache keys read:** `radarr.tags.{instance}` (in `should_auto_unmonitor`, to resolve tag labels).
**dry_run:** **NOT honored in this file.** The PUT/editor APPLY methods mutate Radarr regardless of `self.dry_run`. This is a propagation footgun consistent with the project-wide note that leaf managers must guard their own writes; here the rules-layer sibling enforces dry_run, not this manager. Callers relying on dry_run safety should go through `RadarrMonitoringRulesManager` rather than calling these PUTs directly.

## How it functions

Lifecycle: `__init__` → `super().__init__` (shared deps, parent auto-link) → `self.register()` → resolve `radarr_api`/`instance_manager`/`dry_run`. No `load_components`.

Control flow is request-per-call: each public method resolves the instance, hits the Radarr API, and either returns parsed data or applies a PUT. `batch_update_monitoring` is the only stateful flow with compensating rollback. The `batch_*` cutoff methods read the whole movie list once, compute an id list with a comprehension/loop, then do a single bulk PUT.

No decision is delegated to a `machine_learning` brain module. The rating/tag predicates here are inline heuristics.

## Criteria & examples

- **Auto-monitor (`should_auto_monitor`)** — a movie whose TMDb ratings show `votes = 1500, value = 8.4` returns True (1500 ≥ 1000 and 8.4 ≥ 8.0). A movie with `votes = 1500, value = 7.9` returns False (7.9 < 8.0).
- **Auto-unmonitor (`should_auto_unmonitor`)** — a movie with `votes = 800, value = 3.5` returns True (800 ≥ 500 and 3.5 ≤ 4.0). A movie carrying a tag labeled `"worst-of-2019"` returns True via the blacklist keyword `worst` even with no ratings signal.
- **Cutoff unmonitor (`batch_unmonitor_downloaded_if_cutoff_met`)** — a movie that is `monitored=True, hasFile=True, qualityCutoffNotMet=False`, and not tagged `keep`, is added to the unmonitor batch (its quality goal is satisfied, so nothing more to grab). A movie with `qualityCutoffNotMet=True` is left monitored.
- **Cutoff re-monitor (`batch_monitor_cutoff_unmet`)** — a movie `monitored=False, qualityCutoffNotMet=True` is re-monitored so Radarr keeps hunting for the better file.
- **Rollback** — if `batch_update_monitoring` is asked to set ids `{101: True, 102: True}` and the PUT for 102 throws, 102 is restored to `monitored=False` (`not True`).

## In plain English

This is the hand on the "keep looking for this movie / stop looking" switch. If you tell Radarr you want a movie in 4K but it only grabbed a 1080p copy, that movie stays "monitored" so Radarr keeps watching for an upgrade. Once a movie finally arrives at the quality you wanted (say you have the full-quality Blu-ray rip of The Princess Bride), this flips its switch off so Radarr stops wasting effort hunting for something better. It can do this one movie at a time or for a whole batch at once, and if a batch goes wrong it puts the switches back the way they were.

## Interactions

- **Parent:** `RadarrMonitoringManager` (also called directly by it via `get_monitoring_summary`).
- **Siblings:** `RadarrMonitoringRulesManager` (which owns the dry_run-guarded write path and similar rating heuristics), `RadarrMonitoringSchedulerManager`, `RadarrMonitoringHistoryManager`.
- **Services:** Radarr API (`radarr_api._make_request`), the instance manager (`resolve_instance`), and `global_cache` for tag labels.
- **Brain modules:** none.
