from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit


class SonarrOrchestrationValidatorManager(BaseManager, ComponentManagerMixin):
    """
    Orchestrates full validation, repair, and summary checks for Sonarr instances.
    """

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = self.__class__.__name__.replace("Manager", "")
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

        manager = kwargs.get("manager") or {}
        self.sonarr_cache = kwargs.get("sonarr_cache") or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)
        self.manager = manager or getattr(self, "manager", None)
        self.dry_run = getattr(self.manager, "dry_run", False)

        # SonarrManager exposes SonarrValidatorManager as 'validator_manager'
        # (not 'validator', which is the BaseManager factory validator)
        self.validator = getattr(self.manager, "validator_manager", None)
        if not self.validator:
            self.active = False
            self._inactive_reason = (
                "SonarrValidatorManager (validator_manager) unavailable — "
                "validator orchestration disabled."
            )
            return
        self.active = True

        self.register()
        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    def run_full_validation(self):
        """
        Runs all validation phases: credentials, health, cache audit.
        """
        results = {
            "credentials": self.validator.key_validator.run_credentials_only(),
            "health": self.validator.health_validator.verify_all_instances_health(),
            "auth": self.validator.auth_handler.run_auth_diagnostics() if hasattr(self.validator.auth_handler, "run_auth_diagnostics") else "Not implemented",
            "cache": self.validator.cache_manager.audit_cache_index() if hasattr(self.validator.cache_manager, "audit_cache_index") else "Not implemented"
        }
        self.logger.log_info(f"🧪 Full Validation Results:\n{results}")
        return results

    def run_bootstrap_audit(self):
        """
        Calls validator bootstrap audit (API key presence + health check).
        """
        return self.validator.audit_bootstrap_instances()

    def refresh_credentials(self):
        """
        Prompts user to reconfigure Sonarr instance credentials.
        """
        return self.validator.key_validator.prompt_and_repair_instances()

    def export_all_configs(self, backup_path="./exports/sonarr"):
        """
        Triggers export of all live Sonarr instance configs.
        """
        return self.validator.key_validator.backup_all_configs(backup_path)

    def summarize_validation(self):
        """
        Summarizes registry flags for credential health + component readiness.
        """
        instances = self.config.get("sonarr_instances", {})
        summary = {}

        for name in instances:
            status = {
                "api_present": self.registry.get_flag(f"sonarr.instance.{name}.api_present"),
                "api_missing": self.registry.get_flag(f"sonarr.instance.{name}.api_missing"),
                "health_passed": self.registry.get_flag(f"sonarr.instance.{name}.health_ok"),
                "health_failed": self.registry.get_flag(f"sonarr.instance.{name}.health_fail"),
            }
            summary[name] = status

        self.logger.log_info(f"🧾 Validation Summary:\n{summary}")
        return summary
