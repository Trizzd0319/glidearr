# sonarr/episodes/retrieval/__init__.py

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.services.sonarr.episodes.retrieval.cache import SonarrEpisodesRetrievalCacheManager
from scripts.managers.services.sonarr.episodes.retrieval.enrich import SonarrEpisodesRetrievalEnrichmentManager
from scripts.managers.services.sonarr.episodes.retrieval.fetch import SonarrEpisodesRetrievalFetchManager
from scripts.managers.services.sonarr.episodes.retrieval.sync import SonarrEpisodesRetrievalSyncManager
from scripts.managers.services.sonarr.episodes.retrieval.tvdb import SonarrEpisodesRetrievalTVDBManager
from scripts.managers.services.sonarr.episodes.retrieval.validate import SonarrEpisodesRetrievalValidationManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrEpisodesRetrievalManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrEpisodesRetrieval"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.dry_run = kwargs.get("dry_run", False)
        self.load_summary = {}
        self.parent_name = self.__class__.__name__

        # 🔁 Dual cache setup
        self.global_cache = global_cache
        self.sonarr_cache = kwargs.get("cache_manager") or getattr(kwargs.get("manager", {}), "sonarr_cache", None)

        parent = kwargs.get("manager")
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(parent, "sonarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)

        # 🔩 Subcomponents
        self.components = self.load_components(
            component_map={
                "fetch": SonarrEpisodesRetrievalFetchManager,
                "enrich": SonarrEpisodesRetrievalEnrichmentManager,
                "tvdb": SonarrEpisodesRetrievalTVDBManager,
                "sync": SonarrEpisodesRetrievalSyncManager,
                "validate": SonarrEpisodesRetrievalValidationManager,
                "episode_cache": SonarrEpisodesRetrievalCacheManager,
            },
            registry_prefix="sonarr.episodes.retrieval",
            api_kwarg_name="sonarr_api",
        )

        self.logger.log_debug(f"🧩 SonarrEpisodesRetrievalManager component load complete: {sorted(self.components)}")

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
        self.logger.log_info("🚀 Running SonarrEpisodesRetrievalManager components...")
        for name in self.components:
            component = getattr(self, name, None)
            if component and hasattr(component, "run"):
                try:
                    component.run()
                    self.logger.log_debug(f"✅ Ran: {name}")
                except Exception as e:
                    self.logger.log_error(f"❌ Failed to run '{name}': {e}")
