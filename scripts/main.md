# Main

**File** — `scripts/main.py`
**One-liner** — The process entry point: it builds the factory layer, runs a parallel auth check, constructs every top-level service manager in dependency order, and drives the whole run through its phased `run()` method.

## What it does (for a senior Python engineer)

`class Main(BaseManager, ComponentManagerMixin)` is the top of the manager tree — `parent_name="Main"`, no parent manager above it. It is the orchestrator that owns the run lifecycle; every service manager it constructs auto-links to it as their parent (inheriting its logger/config/cache/validator), so the entire process shares one set of factory dependencies.

It is constructed once at the bottom of the file under `if __name__ == "__main__":` and never instantiated elsewhere in normal operation.

### Responsibilities
- Stand up the factory layer: `LoggerManager`, `ConfigManager` (then `.reload()`), `RegistryManager`, `GlobalCacheManager`, `MetricsLogger`. Each gets a registry flag (`registry_initialized`, `config_initialized`, `cache_initialized`, `metrics_initialized`, `basemanager_initialized`).
- Read `dry_run` from config (`config_manager.get("dry_run", False)`) and propagate it explicitly into every service manager it builds.
- Run a one-shot **parallel auth check** for Radarr/Sonarr/Trakt before any manager exists.
- Construct the top-level service managers in a deliberate order (Tautulli → Trakt → TraktMovies → MAL → Radarr → Sonarr → Acquisition → Writeback → Calendar → SpaceCoordinator).
- Drive the phased run: prepare → run (with cross-service ordering) → space coordinator → plan summary → Phase-3 capabilities → stats roll-up → Discord summary → profiling dump.

### Key public methods
- `__init__(logger=None, config_path=None, config=None, global_cache=None, **kwargs)` — builds the factory layer, calls `super().__init__(...)`, sets `self.dry_run`, runs the auth check (`validate_all`), then calls `_initialize_managers()` and `_validate_managers()`. Accepts pre-built `config` / `global_cache` (used by the `__main__` block, which builds them first).
- `run()` — the entry method; executes the full phased pipeline (see *How it functions*). Returns nothing; side effects are the service runs, cache writes, ledger/plan-summary logging, and the Discord notification.
- `clear_all_service_flags()` — clears registry flags by prefix at the end of a run (`tautulli_`, `trakt_`, `radarr_`, `basemanager_`, `config_`, `cache_`, `metrics_`, `registry_`, `validator_`). Note `sonarr_` is intentionally left commented out, and there is no `radarr_movies`/`mal_`/Phase-3 prefix in the list.

### Internal helpers
- `_initialize_managers()` — constructs each service manager (details below).
- `_validate_managers()` — raises `RuntimeError` if any of `tautulli_initialized`, `trakt_initialized`, `trakt_movies_initialized`, `radarr_initialized` is missing. (Note: `sonarr_initialized` and all Phase-3 flags are NOT in the critical set — Sonarr/Phase-3 are not gated here.)
- `_validate_factories()` — same pattern for the factory flags; defined but not called inside `__init__`.
- `_start_radarr_library_prefetch()` — fire-and-forget background thread that warms the Radarr movie snapshot (see below).

### Where it sits in the manager tree
It is the **root**. It does **not** use `ComponentManagerMixin.load_components` despite mixing the class in — instead it constructs each top-level manager by hand with explicit `**kwargs`, then sets a `<name>_initialized` registry flag per manager. The managers it builds:

| Attr | Class | Module |
|---|---|---|
| `self.tautulli` | `TautulliManager` | `managers.services.tautulli` |
| `self.trakt` | `TraktManager` | `managers.services.trakt` |
| `self.trakt_movies` | `TraktMoviesManager` | `managers.services.trakt.movies` |
| `self.mal` | `MALManager` | `managers.services.mal` |
| `self.radarr` | `RadarrManager` | `managers.services.radarr` |
| `self.sonarr` | `SonarrManager` | `managers.services.sonarr` |
| `self.acquisition` | `AcquisitionManager` | `managers.services.acquisition` |
| `self.writeback` | `WritebackManager` | `managers.services.writeback` |
| `self.calendar` | `CalendarManager` | `managers.services.calendar` |
| `self.space_coordinator` | `SpaceCoordinatorManager` | `managers.services.coordinator` |

The Phase-3 managers (`acquisition`, `writeback`, `calendar`) and the Phase-4 capstone (`space_coordinator`) receive cross-service handles as explicit kwargs — e.g. `writeback` gets `trakt`, `mal`, `sonarr`, `radarr`, `tautulli`; `space_coordinator` gets `sonarr`, `radarr`. `MALManager` is passed via `getattr(self, "mal", None)` so it degrades to `None` if absent.

### FETCH / CACHE / APPLY
`Main` itself performs **none** of the three verbs directly — it is a pure orchestrator that delegates FETCH/CACHE/APPLY to the service managers it owns. The only near-FETCH it does is the **read-only** background `GET /movie` prefetch (warming a cache, not a decision). It is a CACHE *reader* at the end of the run (pulling run-stats keys) and a CACHE *warmer* via size calibration.

### External API endpoints touched
- Indirectly, all service endpoints via the managers it runs.
- Directly: the background prefetch calls `instance_manager.get_movie_library(...)` (or falls back to `im._make_request(name, "movie", fallback=[])`), i.e. Radarr `GET /movie`. Read-only; identical in dry_run and live.
- The auth check (`validate_all`) probes Radarr, Sonarr, and Trakt credentials concurrently.

### Config keys read
- `dry_run` (default `False`).
- `radarr_movie_library_max_age_s` (default `900` seconds) — freshness window for the Radarr snapshot prefetch.
- In the `__main__` block: `daemons.enrich.enabled` (gates the enrichment-daemon respawn and the main-active sentinel).
- `config.raw_data` (or the config itself) is handed whole to `DiscordNotifier`.

### global_cache keys read / written
- **Read** at end of run: `tautulli/run_stats`, `radarr/run_stats`, `sonarr/run_stats`, `trakt/run_stats` (rolled into the run summary).
- **Warmed/read** via `SizeCalibrator.load_into_model()` / `.refresh()` and the background prefetch (`get_movie_library(..., global_cache=self.global_cache)` warms the module-level `_COLLECTION_CACHE` and the on-disk snapshot).
- It does not itself write business-data caches; the service managers do.

### dry_run behavior
`self.dry_run` is read from config and passed into every constructed manager. `Main` does no APPLY of its own, so its only dry_run-sensitive behavior is downstream. Comments document that Phase-3 capabilities and the SpaceCoordinator never write under dry_run (the coordinator still *stamps and persists its selection* into the Parquet ledger even in dry_run, which is the headline value of a dry run). The background movie prefetch is read-only and behaves identically in both modes.

### Singleton / concurrency / threading notes
- As a `BaseManager`, `Main` is a process-wide singleton cached in `_instances`.
- **Auth check** (`validate_all`) uses a `ThreadPoolExecutor(max_workers=3)` to probe the three services in parallel, emitting one summary line.
- **Radarr prefetch** runs on a daemon thread named `radarr-movie-prefetch`, started at the top of `run()` so the ~39s cold `GET /movie` overlaps the Tautulli/Trakt/Sonarr phases. It is `join(timeout=90)`-ed just before `self.radarr.run()`; if it times out or fails, Radarr's repair scans simply do their own live fetch — no regression.

## How it functions

### Lifecycle (`__init__`)
1. Resolve `logger` (default `LoggerManager()`).
2. Build `ConfigManager` (or use injected) and call `.reload()`.
3. Build `RegistryManager`, `GlobalCacheManager` (or injected), `MetricsLogger`; set the four factory flags.
4. `super().__init__(...)` with `parent_name="Main"` — wires the shared deps via `BaseManager`.
5. Read `dry_run`; log `[Main] dry_run=...`; set `basemanager_initialized`.
6. Run the parallel auth check: `from scripts.support.utilities.auth_validator import validate_all` → `self._auth_results = validate_all(config_manager, self.logger)`.
7. `_initialize_managers()` then `_validate_managers()`.

`_initialize_managers()` constructs managers in this order, each followed by `registry.set_flag("<name>_initialized")`. The comments encode the *why* of the ordering: Tautulli first (writes affinity + completions caches); Trakt after Tautulli but before Radarr (so its registry entry is visible to `RadarrOrchestrationManager`); `TraktMoviesManager` self-registers and is picked up from the registry during `run_relational_pull`; MAL self-disables if unauthorized; Radarr and Sonarr next; then the Phase-3 trio and the Phase-4 SpaceCoordinator (each a no-op unless its config flag is set).

### Control flow (`run()`)
1. Create a `RunSummaryCollector(dry_run=...)`.
2. **Size-model warm-load**: build `SizeCalibrator` (from `machine_learning/size_calibration`) and `load_into_model()` so size estimates are accurate from phase 1 (best-effort; on failure `self._size_calibrator=None`).
3. **Start background Radarr prefetch** (`_start_radarr_library_prefetch()`).
4. **Phase 1 — prepare**: `tautulli.prepare()`, `trakt.prepare()`, `radarr.prepare()`, `sonarr.prepare()`.
5. **Phase 2 — run** (each wrapped in try/except that records `summary.add_error(...)` and logs, so one service failing does not abort the run): `tautulli.run()` → `trakt.run()` → `mal.run()` → `sonarr.run()` → (`prefetch.join(timeout=90)`) → `radarr.run()`. Sonarr is run before Radarr specifically so its ~15s wall overlaps the movie prefetch.
6. **Size-calibration refresh** (`self._size_calibrator.refresh()`, TTL-guarded ≈ weekly).
7. **Phase 2.5 — SpaceCoordinator** (`self.space_coordinator.run()`), after both libraries are scored/downgraded but before the plan summary so its unified movie+TV deletes land in the Parquet ledger.
8. **Plan summary**: `PlanSummary(...).log()` (from `machine_learning/plan_summary`) — read-only roll-up of the decision ledger.
9. **Phase 3 — gated capabilities**: loop over `("calendar", "acquisition", "writeback")`, calling `.run()` on each (try/except per manager).
10. **Stats roll-up**: read the four `*/run_stats` keys from `global_cache` and feed `summary`.
11. **Discord summary**: build a `DiscordNotifier(config=..., logger=...)` and `send_run_summary(summary.build())`.
12. **Profiling**: `dump_profile("support/logs/tmp_profile.json")`, `logger.log_profiled_run(...)`, then `clear_all_service_flags()`.

### `__main__` block (process bootstrap, outside the class)
- Builds a `LoggerManager`, then runs `OnboardingManager.run_if_needed(...)` **before** `ConfigManager` is built (so `SecretBootstrap` sees a provisioned keyring and never double-prompts). If it returns `"incomplete"`, it logs guidance and `raise SystemExit(1)`.
- Reads `daemons.enrich.enabled`. If enabled: writes a **main-active sentinel** (`MAIN_ACTIVE_SENTINEL`, JSON `{pid, ts}`) so the enrichment daemon pauses and leaves the shared Trakt rate-limit window to this run, then `EnrichDaemonSupervisor(logger).restart()` (re)spawns a detached daemon. The sentinel is removed in a `finally` block.
- Builds `GlobalCacheManager`, constructs `Main(...)`, and calls `main.run()`.

### Brain delegation
`Main` does not make value judgements. It touches two `machine_learning` modules as orchestration glue (named here, **not documented**): `machine_learning/size_calibration.SizeCalibrator` (warm-load + refresh of the size-model MiB/min overlay) and `machine_learning/plan_summary.PlanSummary` (read-only ledger roll-up). All scoring/deletion/acquisition decisions live inside the service managers and their brain packages, not here.

## Criteria & examples

- **Critical-manager gate** (`_validate_managers`): the four flags `tautulli_initialized`, `trakt_initialized`, `trakt_movies_initialized`, `radarr_initialized` must all be set or `Main` raises `RuntimeError`. Example: if `TraktMoviesManager` fails to construct, `trakt_movies_initialized` is never flagged and the run aborts before Phase 1 — but if **Sonarr** fails to construct, the run continues (Sonarr is deliberately *not* in the critical set).
- **Snapshot freshness window** (`radarr_movie_library_max_age_s`, default `900`): if the persisted Radarr snapshot is younger than 900s, the prefetch reuses it and skips the live `GET /movie` (saving ~39s); if it is 1200s old (> 900), the prefetch does one live fetch and re-stamps the cache. Example: a run that lands 10 minutes (600s < 900s) after the prior run never hits the Radarr API for the movie library.
- **Prefetch join bound** (`prefetch.join(timeout=90)`): if the background fetch hasn't completed within 90s, `Main` proceeds and Radarr's own repair scans fetch the library themselves — bounded so a pathological fetch can never hang the run.
- **Onboarding gate**: `OnboardingManager.run_if_needed(...) == "incomplete"` → exit code 1 with instructions to run `python scripts/support/setup/onboarding.py` or supply `RECOMMENDARR_*` env vars.
- **Daemon gate**: the sentinel write + daemon restart happen only when `daemons.enrich.enabled` is truthy; otherwise that whole bootstrap branch is skipped.

## In plain English

Think of `Main` as the head chef opening the kitchen for the night. Before anyone cooks, the chef checks that the three key suppliers (Radarr, Sonarr, Trakt) are reachable — all at once, with a single "everyone's here" announcement instead of three separate roll-calls. Then the chef sends a runner out early to fetch the big, slow ingredient (the full Radarr movie list, a ~39-second errand) so it arrives while the rest of the prep happens, instead of everyone standing around waiting for it.

The chef doesn't cook anything personally. Instead they line up the stations in the right order — watch-history first, then ratings, then the TV and movie libraries — because each station uses what the previous one prepared. If one station burns a dish, the chef notes it on a clipboard and keeps the rest of dinner going rather than shutting down the whole kitchen.

When everything's plated, the chef writes up the night's report — "here's what we'd serve and what we'd clear off the shelves" — and, in "dry run" mode (a dress rehearsal), nobody actually throws any food away; the chef just shows you the plan. Finally a short message goes out to the group chat (Discord) summarizing how the night went. So for you, the user: this is the one switch that turns on the whole library-curation machine and makes all the pieces run together cleanly and in the right order.

## Interactions

- **Parent manager**: none — `Main` is the root of the tree (`parent_name="Main"`); every service manager auto-links to it as parent.
- **Factory dependencies it builds and shares**: `LoggerManager`, `ConfigManager`, `RegistryManager`, `GlobalCacheManager`, `MetricsLogger`.
- **Service managers it owns/runs**: `TautulliManager`, `TraktManager`, `TraktMoviesManager`, `MALManager`, `RadarrManager`, `SonarrManager`, `AcquisitionManager`, `WritebackManager`, `CalendarManager`, `SpaceCoordinatorManager`.
- **Brain modules it calls (glue only, not documented)**: `machine_learning/size_calibration.SizeCalibrator`, `machine_learning/plan_summary.PlanSummary`.
- **Support utilities**: `auth_validator.validate_all` (parallel auth), `support.notifications.RunSummaryCollector` + `DiscordNotifier` (run report), `support.utilities.decorators.timing.dump_profile` (profiling), `factories.onboarding.OnboardingManager` (first-run setup), `factories.daemons.supervisor.EnrichDaemonSupervisor` + `factories.daemons.daemon_paths.MAIN_ACTIVE_SENTINEL` (enrichment-daemon coordination).
