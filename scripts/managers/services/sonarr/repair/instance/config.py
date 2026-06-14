from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrRepairInstanceConfigManager(BaseManager, ComponentManagerMixin):
    """
    Validates and repairs structural configuration for each Sonarr instance.

    Required keys: base_url, port, api.
    Will fill missing fields with MISSING_<KEY> or fallback defaults in repair mode.
    """

    REQUIRED_KEYS = ["base_url", "port", "api"]

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, manager=None, **kwargs):
        self.parent_name = self.__class__.__name__
        self.manager = manager
        # Capture dry_run before super().__init__ — BaseManager reassigns
        # self.manager to a registry-resolved parent, so reading it at run() time
        # is unreliable (could let a structural repair write to config mid dry-run).
        self.dry_run = kwargs.get("dry_run", getattr(manager, "dry_run", False))
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()
        self.logger.log_debug(f"🛠️ Initialized {self.__class__.__name__} (Parent: {self.parent_name}, Dry Run = {self.dry_run})")

    @LoggerManager().log_function_entry
    @timeit("repair_instance_config_structure")
    def run(self):
        dry_run = self.dry_run
        instances = self.config.get("sonarr_instances", {})
        repaired, skipped, errored = [], [], []

        for name, cfg in instances.items():
            if name == "default_instance" or not isinstance(cfg, dict):
                self.logger.log_debug(f"⏩ Skipping pointer or invalid config for '{name}'")
                continue

            missing_keys = [key for key in self.REQUIRED_KEYS if key not in cfg]

            if not missing_keys:
                self.logger.log_debug(f"✅ Instance '{name}' has complete config.")
                skipped.append(name)
                continue

            self.logger.log_warning(f"⚠️ Instance '{name}' missing keys: {missing_keys}")

            if dry_run:
                self.logger.log_debug(f"💡 [Dry Run] Would repair: {missing_keys} for instance '{name}'")
                continue

            try:
                for key in missing_keys:
                    if key == "port":
                        cfg[key] = 443
                    elif key == "base_url":
                        cfg[key] = "https://REPLACE_ME"
                    elif key == "api":
                        cfg[key] = "MISSING_API_KEY"
                    else:
                        cfg[key] = f"MISSING_{key.upper()}"

                self.registry.set_flag(f"sonarr.instance.{name}.config_repaired", True)
                self.logger.log_debug(f"🔧 Repaired instance '{name}' with defaults: {missing_keys}")
                repaired.append(name)

            except Exception as e:
                self.logger.log_error(f"❌ Failed to repair instance '{name}': {e}")
                errored.append(name)

        self.logger.log_debug(
            f"🧾 Repair Summary: {len(repaired)} repaired, {len(skipped)} valid, {len(errored)} failed"
        )

        return {
            "repaired": repaired,
            "skipped": skipped,
            "errors": errored,
            "success": len(errored) == 0,
        }
