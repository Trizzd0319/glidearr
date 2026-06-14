from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrRepairCacheManager(BaseManager, ComponentManagerMixin):
    def __init__(self, logger=None, config=None, global_cache=None, validator=None,
                 registry=None, cache_manager=None, **kwargs):
        self.parent_name = "SonarrRepair"
        class_name = self.__class__.__name__

        if class_name.endswith("Manager"):
            self.parent_name = class_name.replace("Manager", "")
        else:
            self.parent_name = class_name

        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = self.registry.get("manager", self.parent_name)
        self.logger = self.logger or getattr(parent, "logger", None)
        self.manager = kwargs.get("manager") or getattr(parent, "manager", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        # ✅ Dual cache
        self.sonarr_cache = cache_manager or getattr(parent, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(parent, "global_cache", None)

        if not self.logger:
            raise ValueError(f"❌ {class_name} could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {class_name} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("repair_all_cache")
    def repair_all_cache(self, instance_name=None):
        """
        Attempts to repair all known Sonarr cache types: library, episodes, monitoring, quality, etc.
        If `instance_name` is provided, restricts the repair to keys from that instance.
        """
        self.logger.log_info(f"🧹 Running full cache repair sweep for instance: {instance_name or 'ALL'}")

        repaired = []
        failed = []

        # Only repair keys in the 'sonarr' namespace
        repairable_keys = self.global_cache.get_keys(namespace="sonarr", instance=instance_name)
        for key in repairable_keys:
            try:
                self.global_cache.clear(key)
                repaired.append(key)
                self.logger.log_info(f"✅ Cleared cache key: {key}")
            except Exception as e:
                failed.append((key, str(e)))
                self.logger.log_warning(f"❌ Failed to clear cache key: {key} — {e}")

        self.logger.log_info(f"🧾 Cache Repair Summary: {len(repaired)} repaired, {len(failed)} failed")
        return {"repaired": repaired, "failed": failed}
