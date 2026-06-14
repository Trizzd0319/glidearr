from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrStorageSelectorManager(BaseManager, ComponentManagerMixin):
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "RadarrStorageManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.manager          = parent
        self.dry_run          = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    def _resolve_instance(self, instance):
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    @LoggerManager().log_function_entry
    @timeit("select_instance_by_resolution")
    def select_instance_by_resolution(self, resolution: str) -> str:
        resolution_map = {
            "2160": "4k",
            "1080": "1080",
            "720": "720"
        }

        for key, instance in resolution_map.items():
            if key in resolution:
                self.logger.log_info(f"📺 Resolution '{resolution}' matched to instance '{instance}'")
                return instance

        self.logger.log_warning(f"⚠️ Resolution '{resolution}' not recognized. Defaulting to '720'.")
        return "720"

    @LoggerManager().log_function_entry
    @timeit("select_instance_by_free_space")
    def select_instance_by_free_space(self, required_gb: float = 5.0) -> str:
        from .space import RadarrStorageSpaceManager

        space_manager = RadarrStorageSpaceManager(
            logger=self.logger,
            config=self.config,
            global_cache=self.global_cache,
            validator=self.validator,
            registry=self.registry,
            radarr_api=self.radarr_api,
            instance_manager=self.instance_manager,
            manager=self.manager
        )
        free_space = space_manager.get_free_space_per_instance()
        self.logger.log_debug(f"📦 Free space by instance: {free_space}")

        for instance, space in free_space.items():
            if space >= required_gb:
                self.logger.log_info(f"✅ Selected instance '{instance}' with {space:.2f} GB free.")
                return instance

        if free_space:
            fallback = max(free_space.items(), key=lambda x: x[1])[0]
            self.logger.log_warning(f"⚠️ No instance had enough space. Defaulting to '{fallback}'.")
            return fallback

        self.logger.log_error("❌ No Radarr instance found during storage selection. Defaulting to '720'.")
        return "720"

    @LoggerManager().log_function_entry
    @timeit("select_root_path_for_instance")
    def select_root_path_for_instance(self, instance: str) -> str:
        resolved_instance = self._resolve_instance(instance)
        root_path = self.config.get_radarr_instance_root(resolved_instance)

        if root_path:
            self.logger.log_info(f"📁 Using configured root path for {resolved_instance}: {root_path}")
            return root_path

        from .space import RadarrStorageSpaceManager
        space_manager = RadarrStorageSpaceManager(
            logger=self.logger,
            config=self.config,
            global_cache=self.global_cache,
            validator=self.validator,
            registry=self.registry,
            radarr_api=self.radarr_api,
            instance_manager=self.instance_manager,
            manager=self.manager
        )
        folders = space_manager.get_root_folders(resolved_instance)
        if folders:
            fallback_path = folders[0].get("path", "/tv")
            self.logger.log_info(f"📁 Using fallback root path from Radarr: {fallback_path}")
            return fallback_path

        self.logger.log_warning(f"⚠️ No root path found for instance '{resolved_instance}'. Defaulting to '/tv'.")
        return "/tv"

    @staticmethod
    @LoggerManager().log_function_entry
    @timeit("warm_cache")
    def warm_cache(logger, cache):
        cache.get("radarr/instance/mappings", default=None)
        logger.log_debug("📦 Warmed cache key: radarr/instance/mappings")
