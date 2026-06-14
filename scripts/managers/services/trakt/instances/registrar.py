from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class TraktInstanceRegistrarManager(BaseManager, ComponentManagerMixin):
    parent_name = "TraktManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = "TraktManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()  # ComponentManagerMixin registration

    @LoggerManager().log_function_entry
    @timeit("check_config")
    def check_config(self) -> bool:
        """Validate that all required Trakt config keys are present."""
        trakt_config = (self.config.get("trakt") if self.config else None)
        if not trakt_config:
            self.logger.log_error("[TraktRegistrar] No Trakt configuration found in config.")
            return False

        required_keys = ["client_id", "client_secret", "authorization"]
        missing_keys  = [k for k in required_keys if k not in trakt_config]

        if missing_keys:
            self.logger.log_error(f"[TraktRegistrar] Missing config keys: {', '.join(missing_keys)}")
            return False

        self.logger.log_debug("[TraktRegistrar] Trakt configuration registered successfully.")
        return True
