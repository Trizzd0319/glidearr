from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrOrchestrationSeriesManager(BaseManager, ComponentManagerMixin):
    """
    High-level orchestration for Sonarr Series submodules.
    Delegates to retrieval and sync orchestrators.
    """
    parent_name = "SonarrManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.manager = kwargs.get("manager") or self.registry.get("manager", self.parent_name)
        self.logger = self.logger or getattr(self.manager, "logger", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        self.series_manager = getattr(self.manager, "series", None)
        if not self.series_manager:
            raise ValueError("❌ Missing SonarrSeriesManager reference in orchestration layer.")

        self.retrieval = getattr(self.series_manager, "retrieval", None)
        self.sync = getattr(self.series_manager, "sync", None)

        if not self.retrieval or not self.sync:
            raise ValueError("❌ Retrieval or Sync submanagers not found in SonarrSeriesManager.")

        self.logger.log_debug("🧰 SonarrOrchestrationSeriesManager initialized.")

    @LoggerManager().log_function_entry
    @timeit("run_series_retrieval")
    def run_series_retrieval(self, instance: str = None, full_refresh: bool = True, validate: bool = True):
        """
        Triggers retrieval pipeline for Sonarr series.

        Validation behaviour
        --------------------
        When ``refresh_all_series`` reports that the result came from the disk
        cache (no live API call), the count-validation step is skipped — the
        freshness timestamp already proves the cache was successfully synced
        within the last 24 h, so a redundant live API call to re-count series
        would be wasted work.

        When a live API sync was performed (stale cache, first run, or
        ``force=True``), both count and schema validation run as normal.
        """
        self.logger.log_info(f"🔄 Running series retrieval pipeline (instance={instance})...")

        from_cache = False  # default: assume a sync happened
        if full_refresh:
            result = self.retrieval.fetch.refresh_all_series(instance=instance)
            # refresh_all_series returns (series_list, from_cache: bool)
            if isinstance(result, tuple) and len(result) == 2:
                _, from_cache = result

        if validate:
            if from_cache:
                self.logger.log_info(
                    "⏩ Validation skipped — cache loaded from disk (< 24 h old). "
                    "Series count and schema were confirmed at last sync."
                )
            else:
                self.retrieval.validate.validate_series_count(instance)
                self.retrieval.validate.validate_series_schema(instance)

        self.retrieval.series_cache.persist_letter_cache(instance)

    @LoggerManager().log_function_entry
    @timeit("run_series_sync")
    def run_series_sync(self, instance: str = None, use_tautulli: bool = False, force_all: bool = False):
        """
        Triggers sync logic to reapply tags, monitoring, etc.
        """
        self.logger.log_info(f"🔁 Running composite series sync (instance={instance})...")
        self.sync.composite_sync_workflow(instance=instance, use_tautulli=use_tautulli, force_all=force_all)

    @LoggerManager().log_function_entry
    @timeit("run_episode_file_enrichment")
    def run_episode_file_enrichment(self, instance: str = None):
        """
        Builds and maintains the episode-file Parquet for ML use.

        Two passes per call:

        1. **Pilot batch** — fetches the representative (earliest non-special)
           episode file for each series not yet represented in the Parquet.
           Progress is incremental: up to ``PILOT_BATCH_SIZE`` series per run,
           so large libraries fill in across successive enrichment cycles.

        2. **Tautulli sync** — upserts watched-episode rows using Tautulli
           watch history, enriching those rows with file metadata from Sonarr.

        The method is a no-op (with a warning) when the episode_files manager
        was not registered (e.g. pyarrow not installed, or the component failed
        to initialise).
        """
        self.logger.log_info(f"🎬 Running episode file enrichment for instance: {instance}")

        episode_files_mgr = getattr(
            getattr(self.manager, "sonarr_cache", None),
            "episode_files",
            None,
        )
        if not episode_files_mgr:
            self.logger.log_warning(
                "⚠️ episode_files manager not available — skipping enrichment. "
                "Ensure pyarrow is installed and SonarrCacheEpisodeFilesManager initialised."
            )
            return

        # Resolve instance here so both calls share the same concrete name.
        resolved = self.retrieval.fetch.instance_manager.resolve_instance(instance)

        # Load series list from the shared letter-bucketed cache (no live API call).
        sonarr_cache = getattr(self.manager, "sonarr_cache", None)
        series_cache = getattr(sonarr_cache, "series", None)
        all_series = list(series_cache.iter_all_series(resolved)) if series_cache else []

        if not all_series:
            self.logger.log_warning(
                f"⚠️ No series found in cache for '{resolved}' — "
                "run series retrieval first before episode file enrichment."
            )
            return

        self.logger.log_info(
            f"📚 Episode file enrichment: {len(all_series)} series in '{resolved}'"
        )
        episode_files_mgr.run_pilot_batch(resolved, all_series)
        # Search for missing pilots, stepping down quality profile if previously attempted
        episode_files_mgr.run_pilot_search(resolved)
        # Restore any JIT-upgraded episodes that were watched since last run
        episode_files_mgr.run_jit_quality_restores(resolved)
        # sync_from_tautulli: upserts watched history + computes next_episode
        # + applies 3-h grace period + purges Sonarr-deleted files + cleanup
        episode_files_mgr.sync_from_tautulli(resolved)
        # Upgrade just the upcoming next-episode window to best quality
        episode_files_mgr.run_jit_quality_upgrades(resolved)
        # Compute per-series watchability scores and persist them onto the parquet
        # (the Sonarr twin of Radarr's run_refresh_scores). Runs after the parquet
        # is fully synced so watch aggregates are fresh; feeds the active-watcher
        # upgrade pass and the Phase-3 downgrade / Phase-4 space coordinator.
        # Guarded so a fault in the brand-new scoring path can't abort the rest of
        # series enrichment (e.g. the downstream active-watcher upgrade pass).
        try:
            episode_files_mgr.refresh_scores(resolved)
        except Exception as e:
            self.logger.log_warning(
                f"[ShowScore] refresh_scores failed for '{resolved}': {e}"
            )
        # Broadcast per-series genres + cast/crew + Trakt rating onto the episode rows
        # (from Sonarr + the enrich daemon's show buckets) so the cross-medium next-watch
        # affinity reads TV taste from the same columns as movie_files. Guarded + best-effort.
        try:
            episode_files_mgr.refresh_enrichment(resolved)
        except Exception as e:
            self.logger.log_warning(
                f"[ShowEnrich] refresh_enrichment failed for '{resolved}': {e}"
            )
        # Curative legacy-codec re-grab (gated, default-OFF): AFTER the transcode-decision reports
        # (report_codec_routing runs inside refresh_scores above), swap owned XviD/DivX/MPEG-2 files
        # — which always transcode on modern Plex clients — for a modern-codec release. Budget-capped
        # + cooldown-laddered + dry_run-aware; inert unless scoring.codec_profiles.legacy_regrab=true.
        try:
            episode_files_mgr.regrab_legacy_codecs(resolved)
        except Exception as e:
            self.logger.log_warning(
                f"[LegacyRegrab] regrab_legacy_codecs failed for '{resolved}': {e}"
            )

    @LoggerManager().log_function_entry
    @timeit("run_full_series_enrichment")
    def run_full_series_enrichment(self, instance: str = None):
        """
        Combines full enrichment pass: refresh series + re-sync all attributes
        + episode file metadata for ML use.
        """
        self.logger.log_info(f"🚀 Running full series enrichment for instance: {instance}")
        self.run_series_retrieval(instance=instance, full_refresh=True, validate=True)
        self.run_series_sync(instance=instance, use_tautulli=False, force_all=False)
        self.run_episode_file_enrichment(instance=instance)
        self.run_active_watcher_upgrades(instance=instance)
        # Stage-1 TV downgrade under space pressure — runs AFTER refresh_scores (in
        # run_episode_file_enrichment) so it ranks on fresh watchability scores.
        self.run_space_pressure_downgrades(instance=instance)

    @timeit("run_active_watcher_upgrades")
    def run_active_watcher_upgrades(self, instance: str = None):
        """
        Upgrade quality profiles for actively-watched non-kids series
        when sufficient free space is available.
        """
        # self.manager is the SonarrManager; self.series_manager is its series
        # component (set in __init__). The old code referenced a non-existent
        # self.sonarr_manager attribute.
        resolved = (
            self.manager.instance_manager.resolve_instance(instance)
            if getattr(getattr(self, "manager", None), "instance_manager", None) else instance
        )
        try:
            quality_mgr = getattr(self.series_manager, "quality", None)
            if quality_mgr is None:
                self.logger.log_warning(
                    "[Orchestration] Active-watcher upgrades skipped — series.quality manager unavailable."
                )
                return {}
            stats = quality_mgr.run_active_watcher_upgrades(resolved)
            self.logger.log_info(
                f"[Orchestration] Active-watcher upgrade stats for '{resolved}': {stats}"
            )
            return stats
        except Exception as e:
            self.logger.log_warning(
                f"[Orchestration] Active-watcher upgrades failed for '{resolved}': {e}"
            )
            return {}

    @timeit("run_space_pressure_downgrades")
    def run_space_pressure_downgrades(self, instance: str = None):
        """Stage-1 TV downgrade under space pressure: when free < U, downgrade the
        lowest-watchability series to HD-720p (Phase 3). Gates here (mirroring
        RadarrSpacePressureManager.run): honours ``tv_downgrade_enabled`` and only
        acts under pressure; the manager's run_downgrades does the work."""
        resolved = (
            self.manager.instance_manager.resolve_instance(instance)
            if getattr(getattr(self, "manager", None), "instance_manager", None) else instance
        )
        try:
            if not (self.config or {}).get("tv_downgrade_enabled", True):
                self.logger.log_debug("[Orchestration] TV downgrades disabled (tv_downgrade_enabled=false).")
                return {}
            sp = getattr(self.series_manager, "space_pressure", None)
            if sp is None:
                self.logger.log_warning(
                    "[Orchestration] TV downgrades skipped — series.space_pressure manager unavailable."
                )
                return {}
            free_gb = sp.get_free_space_gb(resolved)
            # Delegate to the manager's total-aware helper so the floor derives from
            # free_space_limit, else 25% of the total drive (disk_total_gb) — never a
            # hardcoded GB fallback unless the total is also unreadable.
            _, U = sp._space_targets(resolved)
            if free_gb >= U:
                self.logger.log_info(
                    f"[Orchestration] TV downgrades: '{resolved}' {free_gb:.0f}GB free ≥ {U:.0f}GB "
                    f"— no space pressure, skipping."
                )
                return {"free_space_gb": free_gb, "action": "none"}
            stats = sp.run_downgrades(resolved, free_gb)
            self.logger.log_info(f"[Orchestration] TV downgrade stats for '{resolved}': {stats}")
            return stats
        except Exception as e:
            self.logger.log_warning(
                f"[Orchestration] TV downgrades failed for '{resolved}': {e}"
            )
            return {}
