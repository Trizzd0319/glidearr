# RadarrMonitoringManager

**File** — `scripts/managers/services/radarr/monitoring/__init__.py`
**One-liner** — The orchestrator that wires together Radarr's four movie-monitoring submanagers (history, movies, rules, scheduler) and exposes a single monitored/unmonitored summary helper.

## What it does (for a senior Python engineer)

`RadarrMonitoringManager(BaseManager, ComponentManagerMixin)` is the parent of the Radarr "monitoring" subtree. It owns no business logic of its own beyond a thin summary helper; its real job is to construct and register the four child managers that handle monitoring concerns.

**Position in the manager tree.** Its `parent_name` is set to its own class name (`"RadarrMonitoringManager"`), and the four submanagers each declare `parent_name = "RadarrMonitoringManager"` so they auto-link back to this instance. It is constructed by the Radarr service manager higher up the tree, which passes in `radarr_api`, `instance_manager`, and `dry_run` via `kwargs` (or this manager falls back to reading them off the parent passed as `kwargs["manager"]`).

**Submanagers loaded.** Unlike the standard `ComponentManagerMixin.load_components` path, this manager builds its children manually so it can split them into critical vs. non-critical groups via `split_components(...)`:

- `history`   → `RadarrMonitoringHistoryManager`
- `movies`    → `RadarrMonitoringMoviesManager`
- `rules`     → `RadarrMonitoringRulesManager`
- `scheduler` → `RadarrMonitoringSchedulerManager`

All four names are in `critical_keys`, so all four are treated as critical. Each is instantiated with a shared `init_kwargs` dict (`logger`, `config`, `global_cache`, `validator`, `registry`, `radarr_api`, `instance_manager`, `manager=self`, `dry_run`), attached as an attribute on `self` (e.g. `self.movies`), and gets a registry flag `radarr.monitoring.<name>_initialized` set True/False. The overall result is recorded as `self.all_components_loaded` and as the registry flag `radarr.monitoring_manager_initialized`. A per-component `self.load_summary` dict captures `"✅ Loaded"` / `"❌ Failed: <err>"`. A filtered component summary is logged via `log_filtered_component_summary(...)`.

**FETCH / CACHE / APPLY.** None directly — this manager delegates everything. The only public verb it exposes is read-only:

- `get_monitoring_summary(instance) -> tuple` — returns `(monitored_list, unmonitored_list)` by calling `self.movies.get_monitored_movies(instance)` and `self.movies.get_unmonitored_movies(instance)` (both FETCH operations under the hood).

**Config keys read:** none directly (children read their own).
**global_cache / Parquet keys:** none directly.
**External API endpoints:** none directly.
**dry_run:** captured from `kwargs`/parent and threaded into every child via `init_kwargs`.
**Singleton/concurrency:** standard `BaseManager` singleton semantics (cached in `_instances`); no threading of its own.

## How it functions

Lifecycle: `__init__` → `super().__init__` (injects shared deps, auto-links parent) → `self.register()` → resolve `radarr_api`/`instance_manager`/`dry_run` → build `init_kwargs` → `split_components(...)` partitions the four classes into critical/non-critical (all four land in critical because they are all in `critical_keys`) → loop-instantiate each, attach to `self`, set the per-component registry flag → set the aggregate `all_components_loaded` flag → emit the filtered component summary.

`split_components` introspects non-critical candidates by temporarily instantiating them and checking their `parent_name`; here that branch is effectively empty since every component is declared critical.

No decision is delegated to a `machine_learning` brain module from this file. (Its children likewise stay rule-based; see their docs.)

## Criteria & examples

The only "rule" here is critical-loading. Example: if `RadarrMonitoringRulesManager` raises during construction, `self.load_summary["rules"]` becomes `"❌ Failed: <error>"`, `registry` flag `radarr.monitoring.rules_initialized` is set False, `all_critical_loaded` flips False, and `radarr.monitoring_manager_initialized` is recorded False — signaling the subtree is degraded even though the other three children loaded.

## In plain English

Think of this as the floor manager of the "is this movie still being watched-for?" department of your movie library. It doesn't personally do any of the work — it just hires and supervises four specialists: one who reads the download history, one who flips the "keep an eye on this movie" switch, one who decides which movies deserve that switch flipped, and one who runs the whole check on a schedule. If you ask the floor manager "what are we currently watching vs. ignoring?", it walks over to the right specialist and reports back.

## Interactions

- **Parent:** the Radarr service manager (passes `radarr_api`, `instance_manager`, `dry_run`).
- **Submanagers (children):** `RadarrMonitoringHistoryManager`, `RadarrMonitoringMoviesManager`, `RadarrMonitoringRulesManager`, `RadarrMonitoringSchedulerManager`.
- **Brain modules:** none.
- **Other services:** indirectly the Radarr API and the instance manager, via its children.
