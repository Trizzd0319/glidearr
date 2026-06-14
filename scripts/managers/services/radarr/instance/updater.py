from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrInstanceUpdaterManager(BaseManager, ComponentManagerMixin):
    """
    Applies corrections and failure-flag updates to the radarr_instances config
    based on validation results from RadarrInstanceManager.
    """

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "RadarrInstanceManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.dry_run          = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    @LoggerManager().log_function_entry
    @timeit("apply_corrections")
    def apply_corrections(self, validation_results: dict) -> dict:
        """
        Updates the radarr_instances config and memory cache based on validation results.
        Marks as failed only on confirmed final failure after all retries.
        Clears failed flag only after confirmed success.
        Always returns the updated config dict.
        """
        radarr_instances = self.config.get("radarr_instances")
        if not isinstance(radarr_instances, dict):
            self.logger.log_error(
                "radarr_instances config is invalid or corrupted (expected dict). Aborting update."
            )
            return {}

        updated = False

        for instance_name, result in validation_results.items():
            instance_config = radarr_instances.get(instance_name)
            if not isinstance(instance_config, dict):
                self.logger.log_warning(
                    f"Skipping correction: '{instance_name}' has invalid config format."
                )
                continue

            if result == "fail":
                if instance_config.get("failed"):
                    self.logger.log_debug(
                        f"Instance '{instance_name}' already marked as failed; skipping update."
                    )
                    continue
                instance_config["failed"] = True
                self.logger.log_warning(
                    f"Marked '{instance_name}' as failed after confirmed final failure."
                )
                updated = True

            elif result in ("success", "recovered"):
                if instance_config.get("failed"):
                    self.logger.log_info(
                        f"Clearing failed flag on '{instance_name}' after confirmed success."
                    )
                    instance_config.pop("failed")
                    updated = True

            else:
                self.logger.log_debug(
                    f"No action needed for '{instance_name}'; result='{result}'."
                )

        if updated:
            self.config.set("radarr_instances", radarr_instances)
            self.logger.log_info("Updated radarr_instances in config with corrections.")

        return radarr_instances
