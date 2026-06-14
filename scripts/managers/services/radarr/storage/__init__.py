from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.cache import CacheKeyBuilder
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.services.radarr.storage.deletion import RadarrStorageDeletionManager
from scripts.managers.services.radarr.storage.library import RadarrLibraryCacheManager
from scripts.managers.services.radarr.storage.relocation import RadarrStorageRelocationManager
from scripts.managers.services.radarr.storage.selection import RadarrStorageSelectorManager
from scripts.managers.services.radarr.storage.space import RadarrStorageSpaceManager
from scripts.support.config.cache_keys import CacheKeyPaths
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.managers.component_splitter import split_components


class RadarrStorageManager(BaseManager, ComponentManagerMixin):
    parent_name = "RadarrStorageManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = __class__.__name__
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.load_summary = {}
        all_critical_loaded = True

        self.global_cache = global_cache
        self.key_builder  = CacheKeyBuilder()
        self.dry_run      = kwargs.get("dry_run", False)

        parent = kwargs.get("manager")
        self.radarr_api      = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)

        init_kwargs = {
            "logger":           self.logger,
            "config":           self.config,
            "global_cache":     self.global_cache,
            "validator":        self.validator,
            "registry":         self.registry,
            "radarr_api":       self.radarr_api,
            "instance_manager": self.instance_manager,
            "manager":          self,
            "dry_run":          self.dry_run,
            "key_builder":      self.key_builder,
        }

        all_component_classes = {
            "deletion":   RadarrStorageDeletionManager,
            "library":    RadarrLibraryCacheManager,
            "selector":   RadarrStorageSelectorManager,
            "space":      RadarrStorageSpaceManager,
            "relocation": RadarrStorageRelocationManager,
        }

        critical_keys = {"space", "library", "selector", "deletion", "relocation"}

        critical_components, noncritical_components = split_components(
            all_components=all_component_classes,
            critical_keys=critical_keys,
            parent_name_match=self.parent_name,
            logger=self.logger,
            logger_context=self.__class__.__name__,
            init_kwargs=init_kwargs,
        )

        for name, cls in critical_components.items():
            try:
                instance = cls(**init_kwargs)
                setattr(self, name, instance)
                self.registry.set_flag(f"radarr.storage.{name}_initialized", True)
                self.load_summary[name] = "✅ Loaded"
            except Exception as e:
                self.registry.set_flag(f"radarr.storage.{name}_initialized", False)
                self.load_summary[name] = f"❌ Failed: {e}"
                all_critical_loaded = False

        for name, cls in noncritical_components.items():
            try:
                instance = cls(**init_kwargs)
                setattr(self, name, instance)
                self.registry.set_flag(f"radarr.storage.{name}_initialized", True)
                self.load_summary[name] = "✅ Loaded"
            except Exception as e:
                self.registry.set_flag(f"radarr.storage.{name}_initialized", False)
                self.load_summary[name] = f"❌ Failed: {e}"

        self.all_components_loaded = all_critical_loaded
        self.registry.set_flag("radarr.storage_manager_initialized", all_critical_loaded)

        self.log_filtered_component_summary(
            service_name="Radarr",
            component_label=self.__class__.__name__,
            critical_components=critical_components.keys(),
            noncritical_components=noncritical_components.keys(),
            all_critical_loaded=all_critical_loaded,
        )

    @LoggerManager().log_function_entry
    @timeit("get_free_space_per_instance")
    def get_free_space_per_instance(self):
        result = {}
        instances = self.config.get_radarr_instances()
        if not instances:
            self.logger.log_warning("No Radarr instance found in config.")
            return result

        for instance in instances:
            resolved_instance = self._resolve_instance(instance)
            root_folders = self.get_root_folders(resolved_instance)

            # Mount-deduped free space; clamp inf (no roots/unreadable) → 0.0 to
            # preserve selection/min() behavior for misconfigured instances.
            _free = self.radarr_api.disk_free_gb(resolved_instance)
            result[resolved_instance] = round(_free if _free != float("inf") else 0.0, 2)
            self.logger.log_debug(f"{resolved_instance} has {result[resolved_instance]} GB free.")
        return result

    @LoggerManager().log_function_entry
    @timeit("get_minimum_free_space")
    def get_minimum_free_space(self):
        space_by_instance = self.get_free_space_per_instance()
        min_space = min(space_by_instance.values()) if space_by_instance else 0
        self.logger.log_info(f"Minimum free space across all instances: {min_space:.2f} GB")
        return min_space

    @LoggerManager().log_function_entry
    @timeit("get_root_folders")
    def get_root_folders(self, instance):
        resolved_instance = self._resolve_instance(instance)
        cache_key = self.key_builder.format_cache_key(
            CacheKeyPaths.radarr.SPACE_ESTIMATES, instance=resolved_instance
        )
        return self.global_cache.get_or_generate_cache(
            key=cache_key,
            generator_function=lambda: self._fetch_root_folders(resolved_instance),
        )

    @LoggerManager().log_function_entry
    @timeit("_fetch_root_folders")
    def _fetch_root_folders(self, instance):
        return self.radarr_api._make_request(instance, "rootfolder", fallback=[])

    def _resolve_instance(self, instance):
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    @staticmethod
    @LoggerManager().log_function_entry
    @timeit("warm_cache")
    def warm_cache(logger, cache, config):
        instance = config.get_default_radarr_instance_name()
        manager  = RadarrStorageSpaceManager(logger=logger, config=config, global_cache=cache)
        key = CacheKeyPaths.radarr.SPACE_ESTIMATES.replace("<instance>", instance or "default")
        cache.get_or_generate_cache(
            key=key,
            generator_function=lambda: manager.get_root_folders(instance),
            expiration_time=300,
        )
