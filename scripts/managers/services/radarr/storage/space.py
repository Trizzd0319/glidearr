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
        # dry_run is resolved by BaseManager (explicit kwarg -> pre-super value ->
        # kwargs["manager"] -> registry parent -> False). The local resolution that used to
        # sit here defaulted to False, silently overwriting a parent-inherited True.
        #: Instances whose free space could NOT be read this run. Kept separate from the
        #: 0.0 they are reported as, because "unreadable" and "full" are different facts and
        #: only one of them should drag an aggregate down (see get_minimum_free_space).
        self._unreadable_instances: set = set()

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

        self._unreadable_instances = set()
        for instance in instances:
            resolved_instance = self.instance_manager.resolve_instance(instance)
            root_folders = self.get_root_folders(resolved_instance)

            # Mount-deduped free space (root folders sharing a disk counted once).
            # Clamp inf (no root folders / unreadable) -> 0.0 so selection/min() treat a
            # misconfigured instance as "no space" and route around it. That is right for
            # SELECTION and wrong for an AGGREGATE, so the two are separated: the 0.0 stays
            # (selection behaviour unchanged) but the instance is also recorded as
            # unreadable, and get_minimum_free_space excludes it. Otherwise one
            # misconfigured instance reports the whole household at 0 GB free -- which,
            # against a 5000 GB floor, reads as maximum space pressure.
            _free = self.radarr_api.disk_free_gb(resolved_instance)
            if _free == float("inf"):
                self._unreadable_instances.add(resolved_instance)
                result[resolved_instance] = 0.0
                self.logger.log_debug(
                    f"📦 {resolved_instance}: free space UNREADABLE (no root folders or "
                    f"unreadable disks) - reported as 0 GB for selection, excluded from the "
                    f"household minimum.")
                continue
            result[resolved_instance] = round(_free, 2)
            self.logger.log_debug(f"📦 {resolved_instance} has {result[resolved_instance]} GB free.")

        return result

    @LoggerManager().log_function_entry
    @timeit("get_minimum_free_space")
    def get_minimum_free_space(self):
        """Least free space across instances whose disks could actually be READ.

        Unreadable instances are reported as 0.0 GB by ``get_free_space_per_instance`` so
        selection routes around them — but including that 0.0 here would let ONE
        misconfigured instance report the entire household as out of space. Against a
        5000 GB floor that reads as maximum pressure, which is the input to the delete path.

        When EVERY instance is unreadable the old value (0.0) is preserved rather than
        invented, because this number may gate deletion and quietly changing it in either
        direction is worse than saying loudly that it cannot be trusted.
        """
        space_by_instance = self.get_free_space_per_instance()
        unreadable = getattr(self, "_unreadable_instances", set())
        readable = {k: v for k, v in space_by_instance.items() if k not in unreadable}

        if not space_by_instance:
            return 0
        if not readable:
            self.logger.log_warning(
                f"⚠️ Free space could not be read on ANY of the {len(space_by_instance)} Radarr "
                f"instance(s) - reporting 0.00 GB, which downstream reads as maximum space "
                f"pressure. Treat this figure as UNKNOWN, not as a full disk.")
            return min(space_by_instance.values())

        min_space = min(readable.values())
        if unreadable:
            self.logger.log_warning(
                f"⚠️ Excluded {len(unreadable)} instance(s) with unreadable free space from the "
                f"household minimum ({', '.join(sorted(unreadable))}) - including them would "
                f"report 0 GB free for the whole household.")
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
