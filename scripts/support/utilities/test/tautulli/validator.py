# beta/managers/services/tautulli/archived_validator.py

from scripts.managers.services.tautulli.api import TautulliAPI

from scripts.managers.factories.cache import GlobalCacheManager
from scripts.managers.factories.config import ConfigManager
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit


class TautulliCacheValidator:
    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, cache=None):
        self.logger = logger or LoggerManager()
        self.config = config or ConfigManager(self.logger)
        self.cache = cache or GlobalCacheManager(logger=self.logger, config=self.config)
        self.api = TautulliAPI(logger=self.logger, config=self.config, cache=self.cache)

    @LoggerManager().log_function_entry
    @timeit("validate")
    def validate(self) -> bool:
        """
        Validate Tautulli connection by calling get_server_friendly_name.
        """
        return self.api.validate()
