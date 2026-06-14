from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrRepairInstanceFlagManager(BaseManager, ComponentManagerMixin):
    """
    Repairs instance-level 'failed' flags in Sonarr config.
    Clears these if reachable or explicitly requested via repair.
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, manager=None, **kwargs):
        self.parent_name = "SonarrRepair"
        self.manager = manager
        self.dry_run = kwargs.get("dry_run", getattr(manager, "dry_run", False))

        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.logger.log_debug(f"🛠️ Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("SonarrRepairInstanceFlagManager.run")
    def run(self):
        """
        Scans config for 'failed' instance flags and clears them if repairable.
        """
        instances = self.config.get("sonarr_instances", {})
        repaired, skipped, failed = [], [], []

        for name, cfg in instances.items():
            if not isinstance(cfg, dict):
                self.logger.log_warning(f"⚠️ Config for instance '{name}' is not a dict — skipping.")
                failed.append(name)
                continue

            if not cfg.get("failed"):
                self.logger.log_debug(f"⏭️ No 'failed' flag on '{name}' — skipping.")
                skipped.append(name)
                continue

            self.logger.log_debug(f"🧹 Found 'failed' flag on '{name}' — attempting to clear")

            if self.dry_run:
                self.logger.log_debug(f"💤 Dry run — would have cleared 'failed' flag for '{name}'")
                continue

            try:
                cfg.pop("failed", None)
                self.registry.set_flag(f"sonarr.instance.{name}.flag_repaired", True)
                repaired.append(name)
                self.logger.log_debug(f"✅ Cleared 'failed' flag for instance '{name}'")
            except Exception as e:
                failed.append(name)
                self.logger.log_error(f"❌ Failed to clear 'failed' flag for '{name}': {e}")

        self.logger.log_debug(
            f"🔧 Flag repair summary: {len(repaired)} repaired, {len(skipped)} skipped, {len(failed)} failed"
        )

        return {
            "repaired": repaired,
            "skipped": skipped,
            "failed": failed,
            "success": len(failed) == 0
        }
