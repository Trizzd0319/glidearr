from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.services.sonarr.series.retrieval.cache import SonarrSeriesRetrievalCacheManager
from scripts.managers.services.sonarr.series.retrieval.enrich import SonarrSeriesRetrievalEnrichManager
from scripts.managers.services.sonarr.series.retrieval.fetch import SonarrSeriesRetrievalFetchManager
from scripts.managers.services.sonarr.series.retrieval.sync import SonarrSeriesRetrievalSyncManager
from scripts.managers.services.sonarr.series.retrieval.tvdb import SonarrSeriesRetrievalTVDBManager
from scripts.managers.services.sonarr.series.retrieval.validate import SonarrSeriesRetrievalValidationManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrSeriesRetrievalManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrSeries"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.manager = kwargs.get("manager") or self.registry.get("manager", self.parent_name)
        self.logger = self.logger or getattr(self.manager, "logger", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))
        self.orchestration = kwargs.get("orchestration") or getattr(self.manager, "orchestration", None)

        self.sonarr_api = kwargs.get("sonarr_api") or getattr(self.manager, "sonarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(self.manager, "instance_manager", None)

        # ✅ Dual-cache structure
        self.global_cache = global_cache or getattr(self.manager, "global_cache", None)
        self.sonarr_cache = kwargs.get("cache_manager") or getattr(self.manager, "sonarr_cache", None)

        # 🔧 Shared kwargs for all subcomponents
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
            "orchestration": self.orchestration,
            "dry_run": self.dry_run,
        }

        # 📦 Load all components
        component_map = {
            "series_cache": SonarrSeriesRetrievalCacheManager,
            "enrich": SonarrSeriesRetrievalEnrichManager,
            "fetch": SonarrSeriesRetrievalFetchManager,
            "sync": SonarrSeriesRetrievalSyncManager,
            "tvdb": SonarrSeriesRetrievalTVDBManager,
            "validate": SonarrSeriesRetrievalValidationManager,
        }

        self.components = self.load_components(
            component_map,
            registry_prefix="sonarr.series.retrieval",
            api_kwarg_name="sonarr_api",
        )

        self.registry.set_flag("sonarr.series.retrieval_manager_initialized", True)
        self.logger.log_debug(f"✅ SonarrSeriesRetrievalManager initialized with: {sorted(self.components)}")

    @LoggerManager().log_function_entry
    @timeit("prepare")
    def prepare(self):
        for name in self.components:
            component = getattr(self, name, None)
            if component and hasattr(component, "prepare"):
                try:
                    component.prepare()
                    self.logger.log_debug(f"✅ Prepared: {name}")
                except Exception as e:
                    self.logger.log_warning(f"⚠️ Failed to prepare '{name}': {e}")

    @LoggerManager().log_function_entry
    @timeit("run")
    def run(self):
        self.logger.log_info("🚀 Running SonarrSeriesRetrievalManager components...")
        for name in self.components:
            component = getattr(self, name, None)
            if component and hasattr(component, "run"):
                try:
                    component.run()
                    self.logger.log_debug(f"✅ Ran: {name}")
                except Exception as e:
                    self.logger.log_error(f"❌ Failed to run '{name}': {e}")
