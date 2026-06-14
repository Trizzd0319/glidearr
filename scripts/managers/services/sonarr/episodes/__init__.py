from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.services.sonarr.episodes.history import SonarrEpisodesHistoryManager
from scripts.managers.services.sonarr.episodes.file import SonarrEpisodesFileManager
from scripts.managers.services.sonarr.episodes.monitoring import SonarrEpisodesMonitoringManager
from scripts.managers.services.sonarr.episodes.retrieval import SonarrEpisodesRetrievalManager
from scripts.managers.services.sonarr.episodes.sharding import SonarrEpisodesShardingManager
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.managers.component_splitter import split_components


class SonarrEpisodesManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrEpisodesManager"

    @LoggerManager().log_function_entry
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.dry_run = kwargs.get("dry_run", False)
        self.load_summary = {}
        self.parent_name = self.__class__.__name__

        # 🔁 Dual cache setup
        self.global_cache = global_cache
        self.sonarr_cache = kwargs.get("cache_manager") or getattr(kwargs.get("manager", {}), "sonarr_cache", None)

        # 🔧 Shared init args for all subcomponents
        init_args = {
            "logger": self.logger,
            "config": self.config,
            "global_cache": self.global_cache,
            "cache_manager": self.sonarr_cache,
            "validator": self.validator,
            "registry": self.registry,
            "manager": self,
            "sonarr_api": kwargs.get("sonarr_api"),
            "instance_manager": kwargs.get("instance_manager"),
            "dry_run": self.dry_run
        }

        all_component_classes = {
            "retrieval": SonarrEpisodesRetrievalManager,
            "file": SonarrEpisodesFileManager,
            "history": SonarrEpisodesHistoryManager,
            "monitoring": SonarrEpisodesMonitoringManager,
            "sharding": SonarrEpisodesShardingManager,
        }

        critical_keys = {"retrieval"}

        critical_components, noncritical_components = split_components(
            all_components=all_component_classes,
            critical_keys=critical_keys,
            parent_name_match=self.parent_name,
            logger=self.logger,
            logger_context=self.__class__.__name__,
            init_kwargs=init_args
        )

        # Load criticals — errors propagate (these are required)
        critical_instances = {name: cls(**init_args) for name, cls in critical_components.items()}
        for name, instance in critical_instances.items():
            setattr(self, name, instance)

        # Load non-criticals — log and skip on failure
        noncritical_instances = {}
        for name, cls in noncritical_components.items():
            try:
                noncritical_instances[name] = cls(**init_args)
                setattr(self, name, noncritical_instances[name])
            except Exception as e:
                self.logger.log_warning(f"⚠️ Non-critical episode component '{name}' failed to initialize: {e}")

        # sharding has parent_name="SonarrEpisodes" which doesn't match the
        # split_components parent_name_match="SonarrEpisodesManager", so it is
        # silently dropped from both dicts.  Load it explicitly here.
        if not getattr(self, "sharding", None):
            try:
                self.sharding = SonarrEpisodesShardingManager(**init_args)
                self.logger.log_debug("🧩 SonarrEpisodesShardingManager loaded explicitly.")
            except Exception as e:
                self.logger.log_warning(f"⚠️ Sharding manager failed to initialize: {e}")
                self.sharding = None

        # Completion flag
        self.all_components_loaded = len(critical_components) == len(critical_instances)
        self.registry.set_flag("sonarr.episodes_manager_initialized", self.all_components_loaded)

        self.log_filtered_component_summary(
            service_name="Sonarr",
            component_label=self.__class__.__name__,
            critical_components=critical_components.keys(),
            noncritical_components=noncritical_components.keys(),
            all_critical_loaded=self.all_components_loaded
        )
