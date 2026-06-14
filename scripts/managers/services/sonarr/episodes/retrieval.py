from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrEpisodesRetrievalManager(BaseManager, ComponentManagerMixin):
    @LoggerManager().log_function_entry
    def __init__(self, logger=None, config=None, global_cache=None, registry=None, manager=None,
                 cache_manager=None, instance_manager=None, dry_run=False, **kwargs):
        class_name = self.__class__.__name__
        self.parent_name = kwargs.get("parent_name") or class_name.replace("Manager", "")

        # Initialize base
        super().__init__(logger, config, global_cache, registry=registry, **kwargs)

        # Resolve parent context
        self.logger = logger or getattr(manager, "logger", None)
        self.manager = manager
        self.dry_run = dry_run
        self.cache_manager = cache_manager or getattr(manager, "cache_manager", None)
        self.instance_manager = instance_manager or getattr(manager, "instance_manager", None)

        if not self.logger:
            raise ValueError("❌ Logger is required for SonarrEpisodeRetrievalManager")

        # Debug instance_manager flow
        # self.logger.log_debug(f"[SonarrEpisodeRetrievalManager.__init__] instance_manager received (arg): {type(instance_manager)} | value: {instance_manager}")
        # self.logger.log_debug(f"[SonarrEpisodeRetrievalManager.__init__] fallback via manager.instance_manager: {getattr(manager, 'instance_manager', None)}")
        # self.logger.log_debug(f"[SonarrEpisodeRetrievalManager.__init__] final resolved self.instance_manager: {self.instance_manager}")

        # Extra visibility for parent managers
        if manager:
            self.logger.log_debug(f"[SonarrEpisodeRetrievalManager.__init__] Parent manager type: {type(manager)} | instance_manager inside parent: {getattr(manager, 'instance_manager', None)}")
        if hasattr(manager, 'episodes'):
            self.logger.log_debug(f"[SonarrEpisodeRetrievalManager.__init__] manager.episodes.instance_manager: {getattr(getattr(manager, 'episodes', None), 'instance_manager', None)}")

        # Instance-aware API validation
        if not self.instance_manager:
            self.logger.log_warning("[SonarrEpisodeRetrievalManager.__init__] ❌ Instance manager not provided.")
        else:
            try:
                all_apis = self.instance_manager.get_all_sonarr_apis()
                instance_keys = list(all_apis.keys())
                self.logger.log_info(f"[SonarrEpisodeRetrievalManager.__init__] ✅ API exposes {len(instance_keys)} Sonarr instances: {instance_keys}")
            except Exception as e:
                self.logger.log_warning(f"[SonarrEpisodeRetrievalManager.__init__] ❌ Failed to access Sonarr APIs from instance_manager: {e}")

    def run_episode_data_pull(self, instance_name):
        self.logger.log_info(f"[SonarrEpisodeRetrievalManager.run_episode_data_pull] 🧪 Would pull episode data for: {instance_name}")
        return True
