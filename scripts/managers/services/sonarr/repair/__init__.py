from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.managers.component_splitter import split_components

# ✅ All repair submodules
from .anomaly import SonarrRepairAnomalyManager
from .cache import SonarrRepairCacheManager
from .episodes import SonarrRepairEpisodesManager
from .file import SonarrRepairFileManager
from .filepaths import SonarrRepairFilepathsManager
from .history import SonarrRepairHistoryManager
from .instance import SonarrRepairInstanceManager
from .metadata import SonarrRepairMetadataManager
from .monitoring import SonarrRepairMonitoringManager
from .orphans import SonarrRepairOrphansManager
from .quality import SonarrRepairQualityManager
from .series import SonarrRepairSeriesManager
from .storage import SonarrRepairStorageManager
from .tags import SonarrRepairTagsManager
from .validator import SonarrRepairValidatorManager


class SonarrRepairManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrManager"

    @LoggerManager().log_function_entry
    @timeit("SonarrRepairManager.__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = self.__class__.__name__
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.sonarr_apis = {}
        self.load_summary = {}
        all_critical_loaded = True

        all_component_classes = {
            "anomaly": SonarrRepairAnomalyManager,
            "repair_cache": SonarrRepairCacheManager,
            "episodes": SonarrRepairEpisodesManager,
            "file": SonarrRepairFileManager,
            "filepaths": SonarrRepairFilepathsManager,
            "history": SonarrRepairHistoryManager,
            "instance": SonarrRepairInstanceManager,
            "metadata": SonarrRepairMetadataManager,
            "monitoring": SonarrRepairMonitoringManager,
            "orphans": SonarrRepairOrphansManager,
            "quality": SonarrRepairQualityManager,
            "series": SonarrRepairSeriesManager,
            "storage": SonarrRepairStorageManager,
            "tags": SonarrRepairTagsManager,
            "validator": SonarrRepairValidatorManager,
        }

        critical_keys = {
            "cache",
            "filepaths",
            "instance",
            "monitoring",
            "storage",
            "validator",
        }

        repair_init_kwargs = {
            "logger":           self.logger,
            "config":           self.config,
            "global_cache":     self.global_cache,
            "validator":        self.validator,
            "registry":         self.registry,
            "manager":          self,
            # Pass through the API + instance refs so sub-managers can resolve
            # their dependencies without raising during split_components introspection.
            "sonarr_api":       kwargs.get("sonarr_api") or getattr(kwargs.get("manager"), "sonarr_api", None),
            "instance_manager": kwargs.get("instance_manager") or getattr(kwargs.get("manager"), "instance_manager", None),
            # Give sub-managers an explicit parent_name so split_components can
            # match them correctly (BaseManager's path inference yields "SonarrRepair",
            # which would never equal the parent_name_match below).
            "parent_name":      self.__class__.__name__,
        }

        critical_components, noncritical_components = split_components(
            all_components=all_component_classes,
            critical_keys=critical_keys,
            # Use __class__.__name__ rather than self.parent_name: BaseManager
            # overwrites self.parent_name with the caller's init_args value
            # ("SonarrManager"), making the match impossible for repair sub-managers.
            parent_name_match=self.__class__.__name__,
            logger=self.logger,
            logger_context=self.__class__.__name__,
            init_kwargs=repair_init_kwargs,
        )

        for name, cls in critical_components.items():
            try:
                instance = cls(**repair_init_kwargs)
                setattr(self, name, instance)
                self.registry.set_flag(f"sonarr.repair.{name}_initialized", True)
                self.load_summary[name] = "✅ Loaded"
            except Exception as e:
                self.registry.set_flag(f"sonarr.repair.{name}_initialized", False)
                self.load_summary[name] = f"❌ Failed: {e}"
                all_critical_loaded = False

        for name, cls in noncritical_components.items():
            try:
                instance = cls(**repair_init_kwargs)
                setattr(self, name, instance)
                self.registry.set_flag(f"sonarr.repair.{name}_initialized", True)
                self.load_summary[name] = "✅ Loaded"
            except Exception as e:
                self.registry.set_flag(f"sonarr.repair.{name}_initialized", False)
                self.load_summary[name] = f"❌ Failed: {e}"

        self.all_components_loaded = all_critical_loaded
        self.registry.set_flag("sonarr.repair_manager_initialized", all_critical_loaded)

        self.log_filtered_component_summary(
            service_name="Sonarr",
            component_label=self.__class__.__name__,
            critical_components=critical_components.keys(),
            noncritical_components=noncritical_components.keys(),
            all_critical_loaded=all_critical_loaded,
        )
