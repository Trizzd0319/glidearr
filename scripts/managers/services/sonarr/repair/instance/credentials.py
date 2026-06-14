from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrRepairInstanceCredentialsManager(BaseManager, ComponentManagerMixin):
    """
    Validates that each Sonarr instance has a usable API key.
    If missing or blank, flags the instance and optionally inserts a placeholder in non-dry-run mode.
    Also supports a lightweight bootstrap-only audit mode.
    """

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, manager=None, **kwargs):
        self.parent_name = __class__.__name__
        self.manager = manager
        # Resolve dry_run from the explicit kwarg / local `manager` param BEFORE
        # super().__init__ runs — BaseManager reassigns self.manager to a
        # registry-resolved parent, so reading self.manager afterwards is unreliable.
        self.dry_run = kwargs.get("dry_run", getattr(manager, "dry_run", False))
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Dry Run = {self.dry_run})")

    @LoggerManager().log_function_entry
    @timeit("repair_instance_credentials")
    def run(self, mode="full"):
        """
        Main entrypoint. Runs a credential repair (`full`) or audit-only (`bootstrap`) check.
        """
        if mode == "bootstrap":
            self.logger.log_debug("🚦 Running credential audit only (bootstrap mode)...")
            return self._run_credentials_only()

        self.logger.log_debug("🛠️ Running full credential repair...")
        instances = self.config.get("sonarr_instances", {})
        valid, missing, repaired, errored = 0, 0, 0, []
        total = len(instances)

        for name, cfg in instances.items():
            if not isinstance(cfg, dict) or name == "default_instance":
                self.logger.log_debug(f"⏩ Skipping pointer or invalid config for '{name}'")
                continue

            try:
                api_key = cfg.get("api")
                flag_path = f"sonarr.instance.{name}.api"

                if not api_key:
                    missing += 1
                    self.logger.log_warning(f"🔑 Missing or empty API key for '{name}'")

                    if not self.dry_run:
                        cfg["failed"] = True
                        self.logger.log_warning(f"🚫 Marked instance '{name}' as failed — API key must be supplied in config before next run.")
                        repaired += 1

                    self.registry.set_flag(f"{flag_path}_missing", True)
                else:
                    valid += 1
                    self.logger.log_debug(f"✅ API key present for '{name}'")
                    self.registry.set_flag(f"{flag_path}_present", True)

            except Exception as e:
                errored.append(name)
                self.logger.log_error(f"❌ Error checking credentials for '{name}': {e}")

        self.logger.log_debug(
            f"🔹 Credential Audit Summary: {valid} valid, {missing} missing, {repaired} repaired, {len(errored)} errored (dry_run={self.dry_run}) out of {total} total instances"
        )

        return {
            "valid": valid,
            "missing": missing,
            "repaired": repaired,
            "errored": errored,
            "success": missing == 0 and len(errored) == 0
        }

    @LoggerManager().log_function_entry
    @timeit("run_credentials_only")
    def _run_credentials_only(self):
        """
        Lightweight check to confirm presence of API keys in config only.
        Does not modify config even in non-dry-run mode.
        """
        instances = self.config.get("sonarr_instances", {})
        valid, missing, errored = 0, 0, []

        for name, cfg in instances.items():
            if not isinstance(cfg, dict) or name == "default_instance":
                self.logger.log_debug(f"⏩ Skipping pointer or invalid config for '{name}'")
                continue

            try:
                api_key = cfg.get("api")
                if not api_key:
                    self.logger.log_warning(f"❌ Missing API key for {name}")
                    self.registry.set_flag(f"sonarr.instance.{name}.api_missing", True)
                    missing += 1
                else:
                    self.logger.log_debug(f"✅ API key present for {name}")
                    self.registry.set_flag(f"sonarr.instance.{name}.api_present", True)
                    valid += 1
            except Exception as e:
                errored.append(name)
                self.logger.log_error(f"❌ Error validating API key for '{name}': {e}")

        self.logger.log_debug(
            f"🔍 Credential Bootstrap Audit: {valid} valid, {missing} missing, {len(errored)} errored"
        )

        return {
            "valid": valid,
            "missing": missing,
            "errored": errored,
            "success": missing == 0 and not errored
        }
