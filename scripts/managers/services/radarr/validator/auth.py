from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrValidatorAuthManager(BaseManager, ComponentManagerMixin):
    """
    Handles authentication validation for Radarr API access.
    Verifies API key availability and optional token refresh stubs.
    """

    parent_name = "RadarrValidatorManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "RadarrValidatorManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.dry_run          = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    @LoggerManager().log_function_entry
    @timeit("validate_api_keys")
    def validate_api_keys(self) -> dict:
        """
        Check that every configured Radarr instance has a non-empty API key in config.
        Returns {instance_name: True/False}.
        """
        radarr_instances = self.config.get("radarr_instances", {})
        results = {}
        for name, cfg in radarr_instances.items():
            key = cfg.get("api") or cfg.get("api_key") or ""
            ok = bool(key.strip())
            if not ok:
                self.logger.log_warning(f"No API key configured for Radarr instance '{name}'")
            results[name] = ok
        return results

    @LoggerManager().log_function_entry
    @timeit("_refresh_token")
    def _refresh_token(self) -> bool:
        """Stub for future OAuth/token refresh support."""
        self.logger.log_info("Token refresh not implemented for Radarr.")
        return False
