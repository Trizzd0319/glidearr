from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.config.cache_keys import CacheKeyPaths as Paths
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrStorageSpaceManager(BaseManager, ComponentManagerMixin):
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
    @timeit("get_free_space_per_instance")
    def get_free_space_per_instance(self):
        result = {}
        if self.instance_manager and hasattr(self.instance_manager, "get_all_radarr_apis"):
            instances = list(self.instance_manager.get_all_radarr_apis().keys())
        else:
            instances = []
        if not instances:
            self.logger.log_warning("⚠️ No Radarr instances found.")
            return result

        for instance in instances:
            resolved_instance = self.instance_manager.resolve_instance(instance)
            root_folders = self.get_root_folders(resolved_instance)

            # Mount-deduped free space (root folders sharing a disk counted once).
            # Clamp inf (no root folders / unreadable) → 0.0 so selection/min()
            # treat a misconfigured instance as "no space", matching prior behavior.
            _free = self.radarr_api.disk_free_gb(resolved_instance)
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
        resolved_instance = self.instance_manager.resolve_instance(instance)
        key = Paths.radarr.SPACE_ESTIMATES.replace("<instance>", resolved_instance)
        return self.global_cache.get_or_generate_cache(
            key=key,
            generator_function=lambda: self._fetch_root_folders(resolved_instance),
        )

    @LoggerManager().log_function_entry
    @timeit("_fetch_root_folders")
    def _fetch_root_folders(self, instance):
        return self.radarr_api._make_request(instance, "rootfolder", fallback=[]) if self.radarr_api else []

    @staticmethod
    @LoggerManager().log_function_entry
    @timeit("warm_cache")
    def warm_cache(logger, cache, config):
        from scripts.managers.services.radarr.storage.space import RadarrStorageSpaceManager
        from scripts.support.config.cache_keys import CacheKeyPaths

        instance = config.get_default_radarr_instance_name()
        manager = RadarrStorageSpaceManager(logger=logger, config=config, global_cache=cache)
        key = CacheKeyPaths.radarr.SPACE_ESTIMATES.replace("<instance>", instance or "default")
        cache.get_or_generate_cache(
            key=key,
            generator_function=lambda: manager.get_root_folders(instance),
            expiration_time=300,
        )
