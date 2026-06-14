from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.cache import CacheKeyBuilder
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.services.sonarr.storage.library import SonarrStorageLibraryManager
from scripts.managers.services.sonarr.storage.selection import SonarrStorageSelectionManager
from scripts.managers.services.sonarr.storage.space import SonarrStorageSpaceManager
from scripts.support.config.cache_keys import CacheKeyPaths
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.managers.component_splitter import split_components


class SonarrStorageManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrStorageManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.dry_run = kwargs.get("dry_run", False)
        self.load_summary = {}
        self.key_builder = CacheKeyBuilder()

        self.global_cache = global_cache
        self.sonarr_cache = kwargs.get("cache_manager") or getattr(kwargs.get("manager", {}), "sonarr_cache", None)

        parent = kwargs.get("manager")
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(parent, "sonarr_api", None)

        init_kwargs = {
            "logger": self.logger,
            "config": self.config,
            "global_cache": self.global_cache,
            "cache_manager": self.sonarr_cache,
            "validator": self.validator,
            "registry": self.registry,
            "manager": self,
            "sonarr_api": self.sonarr_api,
            "key_builder": self.key_builder,
            "dry_run": self.dry_run,
        }

        all_component_classes = {
            "library": SonarrStorageLibraryManager,
            "selection": SonarrStorageSelectionManager,
            "space": SonarrStorageSpaceManager,
        }

        critical_keys = {"space", "library", "selection"}

        critical_components, noncritical_components = split_components(
            all_components=all_component_classes,
            critical_keys=critical_keys,
            parent_name_match=self.parent_name,
            logger=self.logger,
            logger_context=self.__class__.__name__,
            init_kwargs=init_kwargs
        )

        all_critical_loaded = True

        for name, cls in critical_components.items():
            try:
                instance = cls(**init_kwargs)
                setattr(self, name, instance)
                self.registry.set_flag(f"sonarr.storage.{name}_initialized", True)
                self.load_summary[name] = "✅ Loaded"
            except Exception as e:
                self.registry.set_flag(f"sonarr.storage.{name}_initialized", False)
                self.load_summary[name] = f"❌ Failed: {e}"
                all_critical_loaded = False

        for name, cls in noncritical_components.items():
            try:
                instance = cls(**init_kwargs)
                setattr(self, name, instance)
                self.registry.set_flag(f"sonarr.storage.{name}_initialized", True)
                self.load_summary[name] = "✅ Loaded"
            except Exception as e:
                self.registry.set_flag(f"sonarr.storage.{name}_initialized", False)
                self.load_summary[name] = f"❌ Failed: {e}"

        self.all_components_loaded = all_critical_loaded
        self.registry.set_flag("sonarr.storage_manager_initialized", all_critical_loaded)

        self.log_filtered_component_summary(
            service_name="Sonarr",
            component_label=self.__class__.__name__,
            critical_components=critical_components.keys(),
            noncritical_components=noncritical_components.keys(),
            all_critical_loaded=all_critical_loaded
        )

    @LoggerManager().log_function_entry
    @timeit("get_free_space_per_instance")
    def get_free_space_per_instance(self):
        result = {}
        instances = self.config.get_sonarr_instances()
        if not instances:
            self.logger.log_warning("⚠️ No Sonarr instance found in config.")
            return result

        for instance in instances:
            resolved_instance = self.resolve_instance(instance)
            root_folders = self.get_root_folders(resolved_instance)

            # Mount-deduped free space; clamp inf (no roots/unreadable) → 0.0 to
            # preserve selection/min() behavior for misconfigured instances.
            _free = self.sonarr_api.disk_free_gb(resolved_instance)
            result[resolved_instance] = round(_free if _free != float("inf") else 0.0, 2)
            self.logger.log_debug(f"📦 {resolved_instance} has {result[resolved_instance]} GB free.")
        return result

    @LoggerManager().log_function_entry
    @timeit("get_minimum_free_space")
    def get_minimum_free_space(self):
        space_by_instance = self.get_free_space_per_instance()
        min_space = min(space_by_instance.values()) if space_by_instance else 0
        self.logger.log_info(f"📉 Minimum free space across all instance: {min_space:.2f} GB")
        return min_space

    @LoggerManager().log_function_entry
    @timeit("get_root_folders")
    def get_root_folders(self, instance):
        resolved_instance = self.resolve_instance(instance)
        cache_key = self.key_builder.format_cache_key(CacheKeyPaths.sonarr.SPACE_ESTIMATES, instance=resolved_instance)

        return self.global_cache.get_or_generate_cache(
            key=cache_key,
            generator_function=lambda: self._fetch_root_folders(resolved_instance),
        )

    @LoggerManager().log_function_entry
    @timeit("_fetch_root_folders")
    def _fetch_root_folders(self, instance):
        return self.sonarr_api._make_request(instance, "rootfolder", fallback=[])

    @staticmethod
    @LoggerManager().log_function_entry
    @timeit("warm_cache")
    def warm_cache(logger, cache, config):
        from scripts.support.config.cache_keys import CacheKeyPaths
        from scripts.managers.services.sonarr.storage.space import SonarrStorageSpaceManager

        instance = config.get_default_sonarr_instance_name()
        manager = SonarrStorageSpaceManager(logger=logger, config=config, global_cache=cache)
        cache.get_or_generate_cache(
            key=CacheKeyPaths.sonarr.SPACE_ESTIMATES,
            generator_function=lambda: manager.get_root_folders(instance),
            expiration_time=300,
        )