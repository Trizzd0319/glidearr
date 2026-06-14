from datetime import datetime

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class TraktInstanceSummaryFormatterManager(BaseManager, ComponentManagerMixin):
    parent_name = "TraktManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = "TraktManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

    @LoggerManager().log_function_entry
    @timeit("format_summary")
    def format_summary(self) -> dict:
        trakt_config = (self.config.get("trakt") or {}) if self.config else {}
        auth         = trakt_config.get("authorization", {})

        return {
            "username":  trakt_config.get("username") or auth.get("username", "unknown"),
            "client_id": trakt_config.get("client_id", "not set"),
            "token_set": bool(auth.get("access_token")),
            "expires_at": self._format_expiry(
                auth.get("created_at"), auth.get("expires_in")
            ),
        }

    def _format_expiry(self, created_at, expires_in) -> str:
        try:
            if not created_at or not expires_in:
                return "unknown"
            ts = int(created_at) + int(expires_in)
            return datetime.utcfromtimestamp(ts).isoformat() + "Z"
        except Exception:
            return "invalid"
