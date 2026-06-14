from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrInstanceUpdaterManager(BaseManager, ComponentManagerMixin):
    """
    Handles validation correction updates for Sonarr instance configs.
    Flags failures, clears successful retries, and persists to config.
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "SonarrManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

        # 🔧 Dual cache setup
        manager = kwargs.get("manager") or {}
        self.sonarr_cache = kwargs.get("sonarr_cache") or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)

        self.register()

        parent = self.registry.get("manager", self.parent_name)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(parent, "sonarr_api", None)
        self.logger = self.logger or getattr(parent, "logger", None)
        self.manager = manager or getattr(parent, "manager", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        if not self.logger:
            raise ValueError(f"❌ {self.__class__.__name__} could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("apply_corrections")
    def apply_corrections(self, validation_results):
        """
        Applies correction flags to the config.json for failed or recovered Sonarr instances.

        - Marks instances as 'failed' after final validation failure.
        - Clears the 'failed' flag on successful recovery.
        - Does not update intermediate states.
        """
        sonarr_instances = self.config.get("sonarr_instances")
        if not isinstance(sonarr_instances, dict):
            self.logger.log_error("❌ sonarr_instances config is invalid or corrupted (expected dict). Aborting update.")
            return {}

        updated = False

        for instance_name, result in validation_results.items():
            instance_config = sonarr_instances.get(instance_name)
            if not isinstance(instance_config, dict):
                self.logger.log_warning(f"⚠️ Skipping correction: '{instance_name}' has invalid config format (expected dict).")
                continue

            if result == "fail":
                if instance_config.get("failed"):
                    self.logger.log_debug(f"✅ Instance '{instance_name}' already marked as failed; skipping update.")
                    continue

                instance_config["failed"] = True
                self.logger.log_warning(f"⚠️ Marked '{instance_name}' as failed due to confirmed final failure after retries.")
                updated = True

            elif result == "success":
                if instance_config.get("failed"):
                    self.logger.log_debug(f"✅ Clearing failed flag on '{instance_name}' after confirmed final successful validation.")
                    instance_config.pop("failed")
                    updated = True

            else:
                self.logger.log_debug(f"ℹ️ No action needed for '{instance_name}'; result='{result}'.")

        if updated:
            self.config.set("sonarr_instances", sonarr_instances)
            self.logger.log_debug("💾 Updated sonarr_instances in config.json with corrections.")
        else:
            self.logger.log_debug("✅ No corrections needed; all instances clean or already correctly flagged.")

        return sonarr_instances
