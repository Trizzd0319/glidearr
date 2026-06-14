from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit


class SonarrOrchestrationSeriesRetrievalManager(BaseManager, ComponentManagerMixin):
    """
    Orchestrates data retrieval and enrichment for Sonarr series.
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = self.__class__.__name__.replace("Manager", "")

        # 🔧 Dual cache setup
        manager = kwargs.get("manager") or {}
        self.manager = manager
        self.sonarr_cache = kwargs.get("cache_manager") or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)

        super().__init__(logger, config, self.global_cache, validator, registry, **kwargs)
        self.register()

        self.sonarr_api = kwargs.get("sonarr_api") or getattr(manager, "sonarr_api", None)
        self.logger = self.logger or getattr(manager, "logger", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(manager, "instance_manager", None)
        self.dry_run = kwargs.get("dry_run", getattr(manager, "dry_run", False))

        # ✅ Subcomponents from retrieval manager
        self.retrieval = getattr(manager, "retrieval", None)
        self.cache = getattr(self.retrieval, "cache", None)
        self.enrich = getattr(self.retrieval, "enrich", None)
        self.fetch = getattr(self.retrieval, "fetch", None)
        self.sync = getattr(self.retrieval, "sync", None)
        self.tvdb = getattr(self.retrieval, "tvdb", None)
        self.validate = getattr(self.retrieval, "validate", None)

        if not self.logger:
            raise ValueError("❌ SonarrOrchestrationSeriesRetrievalManager could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("run_full_retrieval")
    def run_full_retrieval(self):
        """
        Full enrichment sequence: fetch → enrich → sync → validate
        """
        self.logger.log_info("🚀 Starting full series enrichment and validation...")

        if self.fetch:
            self.fetch.run()

        if self.enrich:
            self.enrich.run()

        if self.sync:
            self.sync.run()

        if self.validate:
            self.validate.validate_series_count("default")
            self.validate.validate_series_schema("default")
            self.validate.validate_series_tags("default")

        self.logger.log_info("🎉 Completed Sonarr series enrichment workflow.")

    @LoggerManager().log_function_entry
    @timeit("run_caching_only")
    def run_caching_only(self):
        """
        Warms series cache via fetch and TVDB only.
        """
        self.logger.log_info("💾 Running series caching warmup...")

        if self.fetch:
            self.fetch.run()

        if self.tvdb:
            self.tvdb.run()

        self.logger.log_info("✅ Series cache warmup complete.")

    @LoggerManager().log_function_entry
    @timeit("run_validation_only")
    def run_validation_only(self):
        """
        Validates series integrity without enrichment.
        """
        self.logger.log_info("🔍 Validating cached series library...")

        if self.validate:
            self.validate.validate_series_count("default")
            self.validate.validate_series_schema("default")
            self.validate.validate_series_tags("default")

        self.logger.log_info("✅ Series validation completed.")
