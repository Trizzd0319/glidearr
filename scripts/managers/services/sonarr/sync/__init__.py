from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.cache import CacheKeyBuilder
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.services.sonarr.sync.custom_formats import SonarrSyncCustomFormatsManager
from scripts.managers.services.sonarr.sync.folders import SonarrSyncFoldersManager
from scripts.managers.services.sonarr.sync.media import SonarrSyncMediaManager
from scripts.managers.services.sonarr.sync.naming import SonarrSyncNamingManager
from scripts.managers.services.sonarr.sync.tags import SonarrSyncTagsManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.managers.component_splitter import split_components


class SonarrSyncManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, sonarr_api=None, **kwargs):
        self.parent_name = __class__.__name__
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.key_builder = CacheKeyBuilder()
        self.sonarr_apis = {}
        self.load_summary = {}
        all_critical_loaded = True

        # Prepare full init args for all subcomponents
        init_args = {
            "logger": self.logger,
            "config": self.config,
            "cache": self.global_cache,
            "validator": self.validator,
            "registry": self.registry,
            "manager": self,
            "sonarr_api": sonarr_api,
            "instance_manager": getattr(sonarr_api, "instance_manager", None),
            "key_builder": self.key_builder,
            "dry_run": kwargs.get("dry_run", False)
        }

        all_component_classes = {
            "custom_formats": SonarrSyncCustomFormatsManager,
            "folders": SonarrSyncFoldersManager,
            "media_management": SonarrSyncMediaManager,
            "naming": SonarrSyncNamingManager,
            "tags": SonarrSyncTagsManager
        }

        critical_keys = {"custom_formats", "folders", "media_management", "naming", "tags"}

        critical_components, noncritical_components = split_components(
            all_components=all_component_classes,
            critical_keys=critical_keys,
            parent_name_match=self.parent_name,
            logger=self.logger,
            logger_context=self.__class__.__name__,
            init_kwargs=init_args
        )

        # Load critical components
        for name, cls in critical_components.items():
            try:
                instance = cls(**init_args)
                setattr(self, name, instance)
                self.registry.set_flag(f"sonarr.sync.{name}_initialized", True)
                self.load_summary[name] = "✅ Loaded"
            except Exception as e:
                self.registry.set_flag(f"sonarr.sync.{name}_initialized", False)
                self.load_summary[name] = f"❌ Failed: {e}"
                all_critical_loaded = False

        # Load noncritical components
        for name, cls in noncritical_components.items():
            try:
                instance = cls(**init_args)
                setattr(self, name, instance)
                self.registry.set_flag(f"sonarr.sync.{name}_initialized", True)
                self.load_summary[name] = "✅ Loaded"
            except Exception as e:
                self.registry.set_flag(f"sonarr.sync.{name}_initialized", False)
                self.load_summary[name] = f"❌ Failed: {e}"

        self.all_components_loaded = all_critical_loaded
        self.registry.set_flag("sonarr.sync_manager_initialized", all_critical_loaded)

        self.log_filtered_component_summary(
            service_name="Sonarr",
            component_label=self.__class__.__name__,
            critical_components=critical_components.keys(),
            noncritical_components=noncritical_components.keys(),
            all_critical_loaded=all_critical_loaded
        )
