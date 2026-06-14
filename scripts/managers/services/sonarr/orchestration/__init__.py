from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.services.sonarr.orchestration.cache import SonarrOrchestrationCacheManager
from scripts.managers.services.sonarr.orchestration.episodes import SonarrOrchestrationEpisodesManager
from scripts.managers.services.sonarr.orchestration.episodes_retrieval import SonarrOrchestrationEpisodeRetrievalManager
from scripts.managers.services.sonarr.orchestration.instance import SonarrOrchestrationInstanceManager
from scripts.managers.services.sonarr.orchestration.monitoring import SonarrOrchestrationMonitoringManager
from scripts.managers.services.sonarr.orchestration.quality import SonarrOrchestrationQualityManager
from scripts.managers.services.sonarr.orchestration.repair import SonarrOrchestrationRepairManager
from scripts.managers.services.sonarr.orchestration.series import SonarrOrchestrationSeriesManager
from scripts.managers.services.sonarr.orchestration.series_retrieval import SonarrOrchestrationSeriesRetrievalManager
from scripts.managers.services.sonarr.orchestration.series_sync import SonarrOrchestrationSeriesSyncManager
from scripts.managers.services.sonarr.orchestration.validator import SonarrOrchestrationValidatorManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrOrchestrationManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, sonarr_api=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.dry_run = kwargs.get("dry_run", False)
        self.load_summary = {}

        # global_cache is resolved by BaseManager parent-linking; only override if explicitly provided
        self.global_cache = global_cache or self.global_cache
        self.sonarr_cache = kwargs.get("cache_manager") or getattr(kwargs.get("manager") or {}, "sonarr_cache", None)
        self.sonarr_api = sonarr_api or kwargs.get("sonarr_api")
        self.manager = kwargs.get("manager") or self.registry.get("manager", "SonarrManager")

        # Base init args forwarded to all sub-orchestrators
        base_init = {
            "logger": self.logger,
            "config": self.config,
            "global_cache": self.global_cache,
            "cache_manager": self.sonarr_cache,
            "validator": self.validator,
            "registry": self.registry,
            "sonarr_api": self.sonarr_api,
            "dry_run": self.dry_run,
            "key_builder": kwargs.get("key_builder"),
        }

        # Most sub-orchestrators operate against the top-level SonarrManager
        sonarr_init = {**base_init, "manager": self.manager}

        # series_retrieval and series_sync operate against SonarrSeriesManager
        series_manager = getattr(self.manager, "series", None) if self.manager else None
        series_init = {**base_init, "manager": series_manager} if series_manager else sonarr_init

        orchestrator_map = {
            "cache": (SonarrOrchestrationCacheManager, sonarr_init),
            "episodes": (SonarrOrchestrationEpisodesManager, sonarr_init),
            "episodes_retrieval": (SonarrOrchestrationEpisodeRetrievalManager, sonarr_init),
            "instance": (SonarrOrchestrationInstanceManager, sonarr_init),
            "monitoring": (SonarrOrchestrationMonitoringManager, sonarr_init),
            "quality": (SonarrOrchestrationQualityManager, sonarr_init),
            "repair": (SonarrOrchestrationRepairManager, sonarr_init),
            "series": (SonarrOrchestrationSeriesManager, sonarr_init),
            "series_retrieval": (SonarrOrchestrationSeriesRetrievalManager, series_init),
            "series_sync": (SonarrOrchestrationSeriesSyncManager, series_init),
            "validator": (SonarrOrchestrationValidatorManager, sonarr_init),
        }

        for name, (cls, init_kwargs) in orchestrator_map.items():
            try:
                instance = cls(**init_kwargs)
                if not getattr(instance, "active", True):
                    # Sub-manager soft-disabled itself (missing dependency, not an error)
                    reason = getattr(instance, "_inactive_reason", "dependency unavailable")
                    self.logger.log_debug(f"⏭️ Orchestration sub-component '{name}' inactive: {reason}")
                    setattr(self, name, None)
                    self.load_summary[name] = f"⏭️ Inactive: {reason}"
                else:
                    setattr(self, name, instance)
                    self.load_summary[name] = "✅ Loaded"
            except Exception as e:
                self.logger.log_warning(f"⚠️ Orchestration sub-component '{name}' skipped: {e}")
                setattr(self, name, None)
                self.load_summary[name] = f"⚠️ Skipped: {e}"

        loaded_count = sum(1 for v in self.load_summary.values() if v.startswith("✅"))
        self.logger.log_debug(
            f"🧩 SonarrOrchestrationManager: {loaded_count}/{len(orchestrator_map)} sub-orchestrators loaded."
        )

    @LoggerManager().log_function_entry
    @timeit("run_full_enrichment")
    def run_full_enrichment(self):
        """Execute the full Sonarr enrichment pipeline: series → episodes."""
        self.logger.log_info("🚀 Starting full Sonarr enrichment pipeline...")

        if self.series:
            try:
                self.series.run_full_series_enrichment()
            except Exception as e:
                self.logger.log_warning(f"⚠️ Series enrichment failed: {e}")

        if self.episodes:
            try:
                self.episodes.run_full_episode_retrieval()
            except Exception as e:
                self.logger.log_warning(f"⚠️ Episode retrieval failed: {e}")

        self.logger.log_info("🎉 Full Sonarr enrichment pipeline completed.")

    @LoggerManager().log_function_entry
    @timeit("run")
    def run(self):
        self.run_full_enrichment()
