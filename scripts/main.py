import sys
import time as _time
import warnings
from pathlib import Path

# Make ``scripts.*`` importable regardless of how this entrypoint is launched
# (Windows Task Scheduler, cron, a Tautulli helper, an IDE) — no PYTHONPATH
# required. Mirrors the self-bootstrap in scripts/support/daemons/enrich_daemon.py
# and scripts/support/setup/onboarding.py.
_REPO_ROOT = Path(__file__).resolve().parents[1]  # repo root (file: scripts/main.py)
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.managers.services.sonarr import SonarrManager  # noqa: E402

warnings.filterwarnings("ignore", category=UserWarning, module="arrapi")

# Pin arrapi's logger so its DEBUG request/response logging (which can include
# credentialed URLs + PII) can never activate, even if root logging goes to DEBUG.
import logging as _logging
_logging.getLogger("arrapi").setLevel(_logging.WARNING)
_logging.getLogger("arrapi").propagate = False

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.cache import GlobalCacheManager
from scripts.managers.factories.config.__Init__ import ConfigManager
from scripts.managers.factories.metrics import MetricsLogger
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.factories.registry import RegistryManager
from scripts.managers.services.plex import PlexManager
from scripts.managers.services.radarr import RadarrManager
from scripts.managers.services.tautulli import TautulliManager
from scripts.managers.services.trakt import TraktManager
from scripts.managers.services.trakt.movies import TraktMoviesManager  # Radarr people enrichment
from scripts.support.notifications import DiscordNotifier, RunSummaryCollector
from scripts.support.utilities.decorators.timing import dump_profile
from scripts.support.utilities.logger.logger import LoggerManager


class Main(BaseManager, ComponentManagerMixin):
    def __init__(self, logger=None, config_path=None, config=None, global_cache=None, **kwargs):
        logger = logger or LoggerManager()

        config_manager = config or ConfigManager(logger=logger)
        config_manager.reload()

        registry = RegistryManager()
        global_cache_manager = global_cache or GlobalCacheManager(logger=logger, config=config_manager)
        metrics_logger = MetricsLogger(logger=logger)

        registry.set_flag("registry_initialized")
        registry.set_flag("config_initialized")
        registry.set_flag("cache_initialized")
        registry.set_flag("metrics_initialized")

        super().__init__(
            logger=logger,
            config=config_manager,
            global_cache=global_cache_manager,
            registry=registry,
            metrics=metrics_logger,
            parent_name="Main",
            **kwargs
        )

        # Fail-SAFE default: if the key is ever absent (fresh/minimal config, a merge
        # that drops it, a typo), assume dry_run=True so the app never silently runs
        # LIVE — the operator must explicitly set dry_run=false to enable writes.
        self.dry_run = config_manager.get("dry_run", True)
        self.logger.log_info(f"[Main] dry_run={self.dry_run}")
        self.registry.set_flag("basemanager_initialized")

        # CONSTRUCT all service managers (internal). VALIDATION is a separate, explicit
        # gate (self.validate()) run after config is loaded/generated and BEFORE any
        # run phase — keeping construction and validation apart makes the startup order
        # unambiguous: load/generate config → construct → validate → run.
        self._auth_results: dict = {}
        self._initialize_managers()

    def _initialize_managers(self):
        # ── Tautulli ──────────────────────────────────────────────────────
        self.tautulli = TautulliManager(
            logger=self.logger,
            config=self.config,
            global_cache=self.global_cache,
            validator=self.validator,
            registry=self.registry,
            dry_run=self.dry_run,
        )
        self.registry.set_flag("tautulli_initialized")

        # ── Plex (optional, NON-critical; constructed AFTER Tautulli so the
        #    identity crosswalk can read tautulli/users + rating_groups, and
        #    BEFORE Trakt so its inventory pass writes plex/watchlist/union warm
        #    for acquisition. Self-disables when unconfigured/unreachable/scope-
        #    fails — deliberately left OUT of _validate_managers, like MAL). ──
        self.plex = PlexManager(
            logger=self.logger,
            config=self.config,
            global_cache=self.global_cache,
            validator=self.validator,
            registry=self.registry,
            dry_run=self.dry_run,
        )
        self.registry.set_flag("plex_initialized")

        # ── Trakt (must come after Tautulli; before Radarr so its registry
        #    entry is visible to RadarrOrchestrationManager) ────────────────
        self.trakt = TraktManager(
            logger=self.logger,
            config=self.config,
            global_cache=self.global_cache,
            validator=self.validator,
            registry=self.registry,
            dry_run=self.dry_run,
        )
        self.registry.set_flag("trakt_initialized")

        # TraktMoviesManager registers itself; RadarrOrchestrationManager picks it
        # up automatically from the registry during run_relational_pull.
        self.trakt_movies = TraktMoviesManager(
            logger=self.logger,
            config=self.config,
            global_cache=self.global_cache,
            validator=self.validator,
            registry=self.registry,
            dry_run=self.dry_run,
        )
        self.registry.set_flag("trakt_movies_initialized")

        # ── MAL (anime; optional — self-disables if not authorized) ─────────
        from scripts.managers.services.mal import MALManager
        self.mal = MALManager(
            logger=self.logger,
            config=self.config,
            global_cache=self.global_cache,
            validator=self.validator,
            registry=self.registry,
            dry_run=self.dry_run,
        )
        self.registry.set_flag("mal_initialized")

        # ── Radarr ────────────────────────────────────────────────────────
        self.radarr = RadarrManager(
            logger=self.logger,
            config=self.config,
            global_cache=self.global_cache,
            validator=self.validator,
            registry=self.registry,
            dry_run=self.dry_run,
        )
        self.registry.set_flag("radarr_initialized")

        # ── Sonarr ────────────────────────────────────────────────────────
        self.sonarr = SonarrManager(
            logger=self.logger,
            config=self.config,
            global_cache=self.global_cache,
            validator=self.validator,
            registry=self.registry,
            dry_run=self.dry_run,
        )
        self.registry.set_flag("sonarr_initialized")

        # ── Phase-3 capabilities (opt-in; constructed after Sonarr/Radarr so
        #    they can use both libraries). Each no-ops unless its config flag
        #    is set, and never writes under dry_run. ────────────────────────
        from scripts.managers.services.acquisition import AcquisitionManager
        self.acquisition = AcquisitionManager(
            logger=self.logger,
            config=self.config,
            global_cache=self.global_cache,
            validator=self.validator,
            registry=self.registry,
            dry_run=self.dry_run,
            trakt=self.trakt,
            mal=getattr(self, "mal", None),
            plex=getattr(self, "plex", None),
            sonarr=self.sonarr,
            radarr=self.radarr,
        )
        from scripts.managers.services.writeback import WritebackManager
        self.writeback = WritebackManager(
            logger=self.logger,
            config=self.config,
            global_cache=self.global_cache,
            validator=self.validator,
            registry=self.registry,
            dry_run=self.dry_run,
            trakt=self.trakt,
            mal=getattr(self, "mal", None),
            sonarr=self.sonarr,
            radarr=self.radarr,
            tautulli=self.tautulli,
        )
        self.registry.set_flag("acquisition_initialized")
        self.registry.set_flag("writeback_initialized")

        from scripts.managers.services.calendar import CalendarManager
        self.calendar = CalendarManager(
            logger=self.logger,
            config=self.config,
            global_cache=self.global_cache,
            validator=self.validator,
            registry=self.registry,
            dry_run=self.dry_run,
            trakt=self.trakt,
            sonarr=self.sonarr,
            radarr=self.radarr,
            mal=getattr(self, "mal", None),
        )
        self.registry.set_flag("calendar_initialized")

        # ── Phase-4 capstone: cross-service space coordinator (opt-in) ─────
        # Unifies Radarr movie + Sonarr episode deletion into one ranked pool
        # under shared-mount space pressure. No-op unless space_coordinator_enabled
        # AND free_space_limit>0; never deletes under dry_run.
        from scripts.managers.services.coordinator import SpaceCoordinatorManager
        self.space_coordinator = SpaceCoordinatorManager(
            logger=self.logger,
            config=self.config,
            global_cache=self.global_cache,
            validator=self.validator,
            registry=self.registry,
            dry_run=self.dry_run,
            sonarr=self.sonarr,
            radarr=self.radarr,
        )
        self.registry.set_flag("space_coordinator_initialized")

    def validate(self):
        """The single validation GATE — run after config is loaded/generated and
        BEFORE any manager runs. Validates, in order:
          1. factories + config infra (registry/config/cache/metrics/basemanager flags),
          2. EXTERNAL service connectivity (the parallel [Auth] check),
          3. the constructed managers (internal init flags),
          4. the *arr REST APIs up front (internal + external),
        so nothing ever runs against an unvalidated stack. Raises on a critical
        failure (factories / required managers)."""
        self.logger.log_info(
            "Validating config + factories + managers + services (internal & external)...")
        self._validate_factories()
        from scripts.support.utilities.auth_validator import validate_all
        self._auth_results = validate_all(self.config, self.logger)
        self._validate_managers()
        self._validate_service_apis()
        self._log_validation_table()
        self.logger.log_success("Validation complete — services ready.")

    def _validate_factories(self):
        for flag in [
            "registry_initialized", "config_initialized", "cache_initialized",
            "metrics_initialized", "basemanager_initialized",
        ]:
            if not self.registry.has_flag(flag):
                raise RuntimeError(f"Critical: Factory with flag '{flag}' failed initialization.")

    def _validate_managers(self):
        for flag in ["tautulli_initialized", "trakt_initialized", "trakt_movies_initialized", "radarr_initialized"]:
            if not self.registry.has_flag(flag):
                raise RuntimeError(f"Critical: Manager with flag '{flag}' failed initialization.")

    def _validate_service_apis(self):
        """Validate the *arr REST APIs UP FRONT — right after config load + manager
        validation — so a validated client is available to EVERY phase, not just after
        a service's own Phase-2 run. Sonarr defers its instance validation to run()
        (Phase 2, after Plex), which left earlier cross-service consumers hitting
        'No validated API'. Hoisting it here removes that phase-order dependency; the
        Phase-2 instance_manager.run() then no-ops (idempotent). Radarr already
        validates at construction. Non-fatal: a failure here is logged and retried in
        the service's own run()."""
        try:
            inst = getattr(getattr(self, "sonarr", None), "instance_manager", None)
            if inst is not None:
                inst.run()
        except Exception as e:
            self.logger.log_warning(f"[Main] Sonarr API pre-validation deferred to run(): {e}")

    def _log_validation_table(self):
        """One organized grid of every validated service (internal & external),
        replacing the scattered per-service connectivity/finalize log lines."""
        rows = []
        for svc, r in (self._auth_results or {}).items():
            if not isinstance(r, dict):
                continue
            status = "OK" if r.get("ok") else "FAIL"
            detail = str(r.get("version") or r.get("label") or r.get("detail") or "")
            rows.append([str(svc).capitalize(), status, detail])
        rows.sort()
        self.logger.log_grid(["Service", "Status", "Detail"], rows,
                             title="Validation - services (internal & external)", cap=28)

    def run(self):
        summary = RunSummaryCollector(dry_run=self.dry_run)

        # Warm the size-model MiB/min overlay from the last run's calibration
        # (instant cache read) so space-pressure / acquisition size estimates are
        # accurate from the first phase. Refreshed from fresh caches after Phase 2.
        try:
            from scripts.managers.machine_learning.size_calibration import SizeCalibrator
            self._size_calibrator = SizeCalibrator(
                registry=self.registry, global_cache=self.global_cache,
                logger=self.logger, config=self.config,
            )
            self._size_calibrator.load_into_model()
        except Exception as e:
            self._size_calibrator = None
            self.logger.log_debug(f"[Main] size calibration warm-load skipped: {e}")

        # Kick off the Radarr full-library GET /movie in the background NOW so its
        # single ~39s cold fetch overlaps every other phase (prepare + Tautulli +
        # Trakt + Sonarr) and lands in the run-scoped snapshot cache before Radarr's
        # repair scans read it. Joined just before Radarr runs (see Phase 2 below).
        prefetch = self._start_radarr_library_prefetch()

        # ── Phase 1: prepare all services ────────────────────────────────
        self.logger.log_info("Phase 1 — preparing all services...")
        self.tautulli.prepare()
        self.trakt.prepare()
        self.radarr.prepare()
        self.sonarr.prepare()
        try:
            self.plex.prepare()
        except Exception as e:
            self.logger.log_warning(f"[Main] Plex prepare skipped: {e}")
        # ── Prepared-managers table (one grid; per-manager load chatter is DEBUG).
        #    Inlined in run() — NOT a helper — so it can't split the method body and
        #    Phase 2's `summary`/`prefetch` locals stay in scope. ──────────────────
        _prep_rows = []
        for _label, _mgr in (("Tautulli", getattr(self, "tautulli", None)),
                             ("Trakt", getattr(self, "trakt", None)),
                             ("Radarr", getattr(self, "radarr", None)),
                             ("Sonarr", getattr(self, "sonarr", None)),
                             ("Plex", getattr(self, "plex", None))):
            if _mgr is None:
                continue
            _ls = getattr(_mgr, "load_summary", {}) or {}
            _total = len(_ls)
            _ok = sum(1 for v in _ls.values() if str(v).startswith("✅"))
            _loaded = f"{_ok}/{_total}" if _total else "n/a"
            _status = "OK" if (_total and _ok == _total) else ("PARTIAL" if _total else "-")
            _prep_rows.append([_label, _loaded, _status])
        self.logger.log_grid(["Service", "Loaded", "Status"], _prep_rows,
                             title="Prepared - managers", cap=20)

        # ── Phase 2: run all services ─────────────────────────────────────
        # Ordered as a producer→consumer pipeline, verified against the actual
        # cross-service global_cache reads (not just intent):
        #   SOURCES   Tautulli → tautulli/affinity + tautulli/group/*/tmdb_completions
        #             Trakt    → trakt/history/movies (priority set for Radarr enrich)
        #             MAL      → mal/* (consumed only in Phase 3)
        #   LIBRARIES Sonarr + Radarr are independent of each other (verified:
        #             neither reads the other's keys). Sonarr runs first so its ~15s
        #             wall overlaps the background Radarr movie prefetch, hiding
        #             Radarr's cold full-library fetch behind work that has to happen
        #             anyway; Radarr is joined to the prefetch and runs last.
        #   PLEX      both passes (run + run_reconcile) sit together, after the *arr.
        #             Plex reads nothing from Trakt, and its only forward consumer
        #             (acquisition's plex/watchlist/union, Phase 3) comes later — so
        #             there is no reason to split the passes across Phase 2 the way
        #             the old order did (inventory early / reconcile late).
        self.logger.log_info("Phase 2 — running all services...")

        try:
            self.tautulli.run()
        except Exception as e:
            summary.add_error(f"Tautulli: {e}")
            self.logger.log_error(f"[Main] Tautulli run failed: {e}")

        try:
            self.trakt.run()
        except Exception as e:
            summary.add_error(f"Trakt: {e}")
            self.logger.log_error(f"[Main] Trakt run failed: {e}")

        try:
            self.mal.run()
        except Exception as e:
            summary.add_error(f"MAL: {e}")
            self.logger.log_error(f"[Main] MAL run failed: {e}")

        # Sonarr is independent of Radarr — run it here so its wall overlaps the
        # background Radarr movie prefetch started at the top of run().
        try:
            self.sonarr.run()
        except Exception as e:
            summary.add_error(f"Sonarr: {e}")
            self.logger.log_error(f"[Main] Sonarr run failed: {e}")

        # Make sure the background movie prefetch has landed in the snapshot cache
        # before Radarr's repair scans read it, so they hit the warm snapshot
        # instead of paying the cold full-library GET. Bounded so a pathological
        # fetch can never hang the run — if it times out, repair just fetches itself.
        if prefetch is not None:
            prefetch.join(timeout=90)

        try:
            self.radarr.run()
        except Exception as e:
            summary.add_error(f"Radarr: {e}")
            self.logger.log_error(f"[Main] Radarr run failed: {e}")

        # ── People↔media co-occurrence matrix (opt-in build; default-off) ─────
        # Reads the enrich daemon's people buckets → a searchable person↔media graph
        # (machine_learning/people_matrix) cached for the scorer's Group-C4 term and
        # the co-cast acquisition source. Pure derived cache — no library writes, no
        # Trakt calls. Gated by people_matrix.enabled (default off); the SCORING /
        # CANDIDATE consumption is separately gated (cap=0.0 / source flag off), so
        # with defaults this is fully inert. Runs after both *arr enrichments so the
        # people buckets are warm, and before Phase 3 so the co-cast source can read it.
        try:
            _pm_cfg = self.config.raw_data if hasattr(self.config, "raw_data") else (self.config or {})
            if (_pm_cfg.get("people_matrix", {}) or {}).get("enabled"):
                from scripts.managers.services.trakt.people_matrix import TraktPeopleMatrixManager
                TraktPeopleMatrixManager(
                    logger=self.logger, config=self.config,
                    global_cache=self.global_cache, registry=self.registry,
                    dry_run=self.dry_run,
                ).build()
        except Exception as e:
            self.logger.log_debug(f"[Main] people-matrix build skipped: {e}")

        # Refresh the size-model calibration from the file caches Sonarr/Radarr
        # just rebuilt (TTL-guarded — recomputes at most once per week, reuses the
        # cached overlay otherwise). Runs before Phase 3 so acquisition's ~size
        # estimates use up-to-date per-quality MiB/min. Best-effort.
        try:
            if getattr(self, "_size_calibrator", None) is not None:
                self._size_calibrator.refresh()
        except Exception as e:
            self.logger.log_debug(f"[Main] size calibration refresh skipped: {e}")

        # ── Plex (single contiguous block, after the *arr) ────────────────
        # Pass 1 — inventory/identity/watchlist. Writes plex/watchlist/union for
        # Phase-3 acquisition and needs Tautulli's user list (already run). It reads
        # nothing the *arr produce, so running it here instead of early is free and
        # keeps both Plex passes together. Plex self-disables when unconfigured.
        try:
            self.plex.run()
        except Exception as e:
            summary.add_error(f"Plex: {e}")
            self.logger.log_error(f"[Main] Plex inventory run failed: {e}")

        # Pass 2 — reconcile: pure zero-API set-diff (orphans/missing) plus playlist
        # plans, now that both *arr libraries + Sonarr JIT state are warm and the
        # Sonarr API is validated. Diagnostic only; never auto-deletes.
        try:
            self.plex.run_reconcile()
        except Exception as e:
            summary.add_error(f"Plex reconcile: {e}")
            self.logger.log_error(f"[Main] Plex reconcile run failed: {e}")

        # ── Phase 2.5: cross-service space coordinator (opt-in; gated) ─────
        # Runs AFTER both services have scored + downgraded, but BEFORE the plan
        # summary so its unified movie+TV delete decisions are stamped into the
        # Parquet caches and rolled up into the ledger below (it stamps + persists
        # its selection even in dry_run). No-op unless space_coordinator_enabled
        # AND free_space_limit>0.
        try:
            if getattr(self, "space_coordinator", None) is not None:
                self.space_coordinator.run()
        except Exception as e:
            summary.add_error(f"SpaceCoordinator: {e}")
            self.logger.log_error(f"[Main] space coordinator run failed: {e}")

        # Roll up the decision ledger (planned_action / watchability_score stamped
        # into the Parquet caches) into one readable "what I'd do" summary — the
        # headline value of a dry_run. Read-only, best-effort.
        try:
            from scripts.managers.machine_learning.plan_summary import PlanSummary
            PlanSummary(registry=self.registry, logger=self.logger, config=self.config).log()
        except Exception as e:
            self.logger.log_debug(f"[Main] plan summary skipped: {e}")

        # ── Phase 2.6: library re-organizer (opt-in; gated) ────────────────
        # Reconciles owned media to the correct library FOLDER. Runs AFTER the space
        # coordinator's delete decisions and BEFORE Phase-3 acquisition. No-op until the
        # routing onboarding step stamped routing.configured; log_only just LOGS misplacements,
        # and same_instance moves are further gated by relocation consent + never run under dry_run.
        try:
            from scripts.managers.services.routing import RoutingManager
            RoutingManager(config=self.config, logger=self.logger,
                           radarr=getattr(self, "radarr", None), sonarr=getattr(self, "sonarr", None),
                           dry_run=self.dry_run).run()
        except Exception as e:
            summary.add_error(f"Routing: {e}")
            self.logger.log_error(f"[Main] routing re-organizer failed: {e}")

        # Dual-version reconcile: mirror standard-instance movies that were upgraded to a 2160p
        # file onto the dedicated 4K instance (so the premium copy lives where it belongs). Inert
        # unless routing.configured + movies.4k_policy=='both' + a distinct 4K instance; log_only
        # just LOGS candidates, same_instance actuates the mirror adds, dry_run never POSTs.
        try:
            from scripts.managers.services.routing.uhd_reconcile import UhdReconcileManager
            UhdReconcileManager(config=self.config, logger=self.logger,
                                radarr=getattr(self, "radarr", None), dry_run=self.dry_run).run()
        except Exception as e:
            summary.add_error(f"UHD reconcile: {e}")
            self.logger.log_error(f"[Main] uhd reconcile failed: {e}")

        # ── Phase 3: acquisition / write-back / calendar (opt-in; gated) ───
        # Each capability is a no-op unless its config flag is enabled and never
        # writes under dry_run, so with defaults this phase changes nothing.
        for name in ("calendar", "acquisition", "writeback"):
            mgr = getattr(self, name, None)
            if mgr is None:
                continue
            try:
                mgr.run()
            except Exception as e:
                summary.add_error(f"{name.capitalize()}: {e}")
                self.logger.log_error(f"[Main] {name} run failed: {e}")

        self.logger.log_info("All services completed.")

        # ── Collect per-service run stats ─────────────────────────────────
        # Managers write their stats to global_cache at run-time under
        # well-known keys so they can be pulled here without coupling Main
        # directly to every manager's return value.
        if self.global_cache:
            try:
                tau_stats = self.global_cache.get("tautulli/run_stats") or {}
                summary.tautulli_history( tau_stats.get("history_entries",  0))
                summary.tautulli_metadata(tau_stats.get("metadata_indexed", 0))
                summary.tautulli_users(   tau_stats.get("users_tracked",    0))
            except Exception:
                pass
            try:
                summary.merge("radarr", self.global_cache.get("radarr/run_stats") or {})
            except Exception:
                pass
            try:
                summary.merge("sonarr", self.global_cache.get("sonarr/run_stats") or {})
            except Exception:
                pass
            try:
                summary.merge("trakt",  self.global_cache.get("trakt/run_stats")  or {})
            except Exception:
                pass
            try:
                plex_stats = self.global_cache.get("plex/run_stats") or {}
                if plex_stats.get("enabled"):
                    summary.merge("plex", {
                        "users": plex_stats.get("users_tracked", 0),
                        "watchlist_items": plex_stats.get("watchlist_items", 0),
                        "scope_ok": plex_stats.get("scope_ok", False),
                        "pin_skipped": plex_stats.get("users_pin_skipped", 0),
                        "calls": plex_stats.get("calls_made", 0),
                    })
            except Exception:
                pass

        # ── Discord health summary ────────────────────────────────────────
        try:
            cfg = self.config.raw_data if hasattr(self.config, "raw_data") else (self.config or {})
            notifier = DiscordNotifier(config=cfg, logger=self.logger)
            notifier.send_run_summary(summary.build())
        except Exception as e:
            self.logger.log_warning(f"[Main] Discord notification failed: {e}")

        dump_profile("support/logs/tmp_profile.json")
        self.logger.log_profiled_run(profile_path="support/logs/tmp_profile.json")
        self.clear_all_service_flags()
        # ── Consolidated end-of-run decision/movement tables ──────────────────
        # Per-title detail that managers recorded during the run (instead of
        # spamming it inline) is rendered here as one group of tables. No-op
        # until managers are wired to global_cache.run_summary. Kept just above
        # the deletions banner so that banner stays the literal last log block.
        try:
            run_summary = getattr(self.global_cache, "run_summary", None) if self.global_cache else None
            if run_summary is not None:
                run_summary.render(self.logger)
        except Exception as e:
            self.logger.log_warning(f"[Main] Run-summary render failed: {e}")
        self._warn_if_deletions_disabled()

    def _start_radarr_library_prefetch(self):
        """
        Warm the Radarr movie snapshot in the background so it is ready before the
        first repair scan reads it. This is the single freshness decision for the
        run: get_movie_library() loads the persistent on-disk snapshot when it is
        younger than ``radarr_movie_library_max_age_s`` (config, default 900s) — so a
        run that lands inside that window skips the ~39s live GET /movie entirely —
        otherwise it does one live fetch (overlapping the Tautulli+Trakt+Sonarr
        phases) and re-stamps the cache for next time. Either way it warms the
        module-level _COLLECTION_CACHE, so the repair scans AND the orchestration
        enrichment transparently reuse the same snapshot.

        Fire-and-forget + best-effort: if it fails or hasn't finished in time, the
        repair scans just do their own live fetch — no regression, no behavior
        change (read-only GET; identical in dry_run/live).

        This was inserted here instead of letting it run during normal ordering due
        delaying activation of script by ~39s. timing-runs showed this lasted from
        ~30-50 seconds on average, causing unneccessary disruption in testing.
        """
        radarr = getattr(self, "radarr", None)
        im = getattr(radarr, "instance_manager", None) or getattr(radarr, "radarr_api", None)
        if im is None or not hasattr(im, "_make_request") or not hasattr(im, "_get_apis"):
            return None

        try:
            max_age_s = int(self.config.get("radarr_movie_library_max_age_s", 900) or 0)
        except Exception:
            max_age_s = 900

        def _worker():
            try:
                names = list((im._get_apis() or {}).keys())
                t0 = _time.monotonic()
                for name in names:
                    if hasattr(im, "get_movie_library"):
                        im.get_movie_library(name, max_age_s=max_age_s, global_cache=self.global_cache)
                    else:
                        im._make_request(name, "movie", fallback=[])
                self.logger.log_debug(
                    f"[Main] Radarr library prefetch warmed {len(names)} instance(s) "
                    f"in {_time.monotonic() - t0:.1f}s (max_age={max_age_s}s)"
                )
            except Exception as e:
                self.logger.log_debug(f"[Main] Radarr library prefetch skipped: {e}")

        import threading
        thread = threading.Thread(target=_worker, name="radarr-movie-prefetch", daemon=True)
        thread.start()
        return thread

    def clear_all_service_flags(self):
        self.registry.clear_flags(prefix="tautulli_")
        self.registry.clear_flags(prefix="trakt_")
        self.registry.clear_flags(prefix="radarr_")
        # self.registry.clear_flags(prefix="sonarr_")
        self.registry.clear_flags(prefix="basemanager_")
        self.registry.clear_flags(prefix="config_")
        self.registry.clear_flags(prefix="cache_")
        self.registry.clear_flags(prefix="metrics_")
        self.registry.clear_flags(prefix="registry_")
        self.registry.clear_flags(prefix="validator_")

    def _warn_if_deletions_disabled(self) -> None:
        """LAST lines of the run log: when free_space_limit is unset (0), every media
        delete pass was skipped this run (deletions_enabled hard gate). Emitted at the
        END so it can't be buried, at EVERY log level so it survives any level filter,
        and printed straight to the console when the session is interactive."""
        from scripts.support.utilities.space_targets import deletions_enabled, deletions_consented
        try:
            if deletions_enabled(self.config):
                return
            if not deletions_consented(self.config):
                banner = (
                    "⛔ DELETIONS DISABLED — you have NOT consented to media deletion. "
                    "Glidearr scored and planned everything (and, where enabled, changed "
                    "quality profiles and built playlists) but did NOT delete any files, "
                    "and never will until you explicitly opt in: the onboarding 'Media "
                    "deletion' step, or set RECOMMENDARR_DELETIONS_CONSENT=true — AND set "
                    "free_space_limit (a GB free-space floor) in config.json."
                )
            else:
                banner = (
                    "⛔ DELETIONS DISABLED — you consented to deletion, but free_space_limit "
                    "is not set (0). Set free_space_limit (a GB free-space floor) in "
                    "config.json to arm reclamation. Downgrades, monitoring, grace marking, "
                    "playlists and acquisition ran normally."
                )
            for _emit in (self.logger.log_debug, self.logger.log_info,
                          self.logger.log_warning, self.logger.log_error):
                try:
                    _emit(f"[Main] {banner}")
                except Exception:
                    pass
            try:
                import sys as _sys
                if _sys.stdout.isatty():
                    print(f"\n{'=' * 78}\n{banner}\n{'=' * 78}")
            except Exception:
                pass
        except Exception:
            pass   # the banner must never crash the end of a run


if __name__ == "__main__":
    logger = LoggerManager()
    # ════════════════════════════════════════════════════════════════════════════
    # STEP 1 — CONFIGURATION: check the config, run setup to GENERATE it if needed,
    #          then load it. Onboarding runs FIRST (before ConfigManager) so it
    #          provisions the keyring that ConfigManager's SecretBootstrap reads —
    #          otherwise SecretBootstrap double-prompts. No-op once configured / when
    #          RECOMMENDARR_SKIP_ONBOARDING is set.
    # ════════════════════════════════════════════════════════════════════════════
    from scripts.managers.factories.onboarding import OnboardingManager
    if OnboardingManager.run_if_needed(logger=logger) == "incomplete":
        logger.log_error(
            "[Main] Setup incomplete — no usable configuration. Run "
            "`python scripts/support/setup/onboarding.py` in an interactive terminal, or provide "
            "RECOMMENDARR_* env vars (see `python scripts/support/setup/onboarding.py --print-env-template`), "
            "then re-run."
        )
        raise SystemExit(1)
    config_manager = ConfigManager(logger=logger)

    _daemon_enabled = bool(
        ((config_manager.get("daemons", {}) or {}).get("enrich") or {}).get("enabled")
    )

    def _write_main_active_sentinel():
        """Mark this run active so the enrichment daemon pauses fetching and leaves
        the shared Trakt rate-limit window to us. Written BEFORE the daemon is
        (re)spawned so the fresh daemon sees it immediately."""
        import json as _json, os as _os, time as _time
        from scripts.managers.factories.daemons.daemon_paths import MAIN_ACTIVE_SENTINEL
        try:
            MAIN_ACTIVE_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
            MAIN_ACTIVE_SENTINEL.write_text(
                _json.dumps({"pid": _os.getpid(), "ts": _time.time()})
            )
        except Exception as e:
            logger.log_warning(f"[Main] could not write main-active sentinel: {e}")

    def _remove_main_active_sentinel():
        from scripts.managers.factories.daemons.daemon_paths import MAIN_ACTIVE_SENTINEL
        try:
            MAIN_ACTIVE_SENTINEL.unlink(missing_ok=True)
        except OSError:
            pass

    if _daemon_enabled:
        _write_main_active_sentinel()
    try:
        # If the background enrichment daemon is enabled, (re)spawn it now:
        # gracefully stop any running instance, then launch a fresh detached
        # process that fetches Trakt metadata out-of-band. This run then enriches
        # cache-only (no live Trakt calls), so it can never hang on a 429. The
        # fresh daemon sees the main-active sentinel above and pauses until we
        # finish, so it never competes for the Trakt rate window mid-run.
        if _daemon_enabled:
            try:
                from scripts.managers.factories.daemons.supervisor import EnrichDaemonSupervisor
                EnrichDaemonSupervisor(logger=logger).restart()
            except Exception as e:
                logger.log_warning(f"[Main] enrich daemon supervisor failed: {e}")

        global_cache = GlobalCacheManager(logger=logger, config=config_manager)

        # ════════════════════════════════════════════════════════════════════════
        # STEP 2 — CONSTRUCT + VALIDATE: build every manager, then run the single
        #          validation gate (config + factories + managers + services, both
        #          INTERNAL and EXTERNAL) before anything else happens.
        # ════════════════════════════════════════════════════════════════════════
        main = Main(logger=logger, config=config_manager, global_cache=global_cache)
        main.validate()

        # ════════════════════════════════════════════════════════════════════════
        # STEP 3 — RUN. (The proper run-ORDER of the managers/factories is a separate,
        #          upcoming rework — to be tackled after the Plex work.)
        # ════════════════════════════════════════════════════════════════════════
        main.run()
    finally:
        if _daemon_enabled:
            _remove_main_active_sentinel()
