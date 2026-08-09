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

        # dry_run is resolved by BaseManager (explicit kwarg -> pre-super value ->
        # kwargs["manager"] -> registry parent -> False). The local
        # `kwargs.get("dry_run", False)` that used to sit here was the WEAKEST form found
        # -- one level, no parent fallback -- and it is passed down to every subcomponent
        # through init_args below, so it set the mode for the whole episodes subtree.
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

        # Completion flag
        # NOTE (GLD-EPI-04): this counts CRITICALS only, and critical_keys is {"retrieval"},
        # so it evaluates 1 == 1 and reports success even when a noncritical component
        # fails. Left as-is deliberately: `sonarr.episodes_manager_initialized` gates the
        # parent's load, and tightening it here would turn a noncritical failure into a
        # failed service. Fix the flag and its consumers together, not one of them.
        self.all_components_loaded = len(critical_components) == len(critical_instances)
        self.registry.set_flag("sonarr.episodes_manager_initialized", self.all_components_loaded)

        self.log_filtered_component_summary(
            service_name="Sonarr",
            component_label=self.__class__.__name__,
            critical_components=critical_components.keys(),
            noncritical_components=noncritical_components.keys(),
            all_critical_loaded=self.all_components_loaded
        )
