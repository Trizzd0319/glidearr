from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.services.sonarr.series.helpers import SonarrSeriesHelpersManager
from scripts.managers.services.sonarr.series.monitoring import SonarrSeriesMonitoringManager
from scripts.managers.services.sonarr.series.quality import SonarrSeriesQualityManager
from scripts.managers.services.sonarr.series.retrieval import SonarrSeriesRetrievalManager
from scripts.managers.services.sonarr.series.space_pressure import SonarrSpacePressureManager
from scripts.managers.services.sonarr.series.sync import SonarrSeriesSyncManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrSeriesManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrSeries"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.dry_run = kwargs.get("dry_run", False)
        self.parent_name = self.__class__.__name__
        self.load_summary = {}

        # ✅ Dual-cache support
        self.global_cache = global_cache
        self.sonarr_cache = kwargs.get("cache_manager") or getattr(kwargs.get("manager", {}), "sonarr_cache", None)

        parent = kwargs.get("manager")
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(parent, "sonarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)

        # 🔧 Init args passed to submanagers
        init_args = {
            "logger": self.logger,
            "config": self.config,
            "global_cache": self.global_cache,
            "cache_manager": self.sonarr_cache,
            "validator": self.validator,
            "registry": self.registry,
            "manager": self,
            "sonarr_api": self.sonarr_api,
            "instance_manager": self.instance_manager,
            "dry_run": self.dry_run
        }

        # 🔩 Subcomponent mapping
        self.components = self.load_components(
            component_map={
                "helpers": SonarrSeriesHelpersManager,
                "retrieval": SonarrSeriesRetrievalManager,
                "monitoring": SonarrSeriesMonitoringManager,
                "quality": SonarrSeriesQualityManager,
                "space_pressure": SonarrSpacePressureManager,
                "sync": SonarrSeriesSyncManager
            },
            registry_prefix="sonarr.series",
            api_kwarg_name="sonarr_api",
        )

        self.logger.log_debug(f"🧩 SonarrSeriesManager component load complete: {sorted(self.components)}")

    @LoggerManager().log_function_entry
    @timeit("prepare")
    def prepare(self):
        for name in self.components:
            comp = getattr(self, name, None)
            if comp and hasattr(comp, "prepare"):
                try:
                    comp.prepare()
                    self.logger.log_debug(f"✅ Prepared: {name}")
                except Exception as e:
                    self.logger.log_warning(f"⚠️ Failed to prepare '{name}': {e}")

    @LoggerManager().log_function_entry
    @timeit("run")
    def run(self):
        self.logger.log_info("🚀 Running SonarrSeriesManager components...")
        for name in self.components:
            comp = getattr(self, name, None)
            if comp and hasattr(comp, "run"):
                try:
                    comp.run()
                    self.logger.log_debug(f"✅ Ran: {name}")
                except Exception as e:
                    self.logger.log_error(f"❌ Failed to run '{name}': {e}")
