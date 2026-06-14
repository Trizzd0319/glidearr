from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrValidatorAuthManager(BaseManager, ComponentManagerMixin):
    """
    Handles API authentication validation and token refresh stub for Sonarr.
    Currently placeholder, pending Sonarr OAuth support.
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = self.__class__.__name__
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

        # 🔧 Dual cache setup
        manager = kwargs.get("manager") or {}
        self.sonarr_cache = kwargs.get("sonarr_cache") or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)

        self.register()

        self.sonarr_api = kwargs.get("sonarr_api") or getattr(manager, "sonarr_api", None)
        self.logger = self.logger or getattr(manager, "logger", None)
        self.manager = manager
        self.dry_run = kwargs.get("dry_run", getattr(manager, "dry_run", False))

        if not self.logger:
            raise ValueError(f"❌ {self.parent_name} could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {self.parent_name} (Dry Run = {self.dry_run})")

    @LoggerManager().log_function_entry
    @timeit("validate_auth_token")
    def _refresh_token(self) -> bool:
        """
        Stub for future token refresh support if Sonarr implements OAuth or API token expiration.
        """
        self.logger.log_info("🔁 Token refresh not implemented for Sonarr.")
        return False
