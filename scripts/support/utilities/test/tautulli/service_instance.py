# tautulli/service_instance.py
from scripts.managers.factories.base_manager import BaseManager


class TautulliServiceInstance(BaseManager):
    def __init__(self, name, config, logger, global_cache, validator, registry, api_config):
        super().__init__(logger, config, global_cache, validator, registry)
        self.name = name
        self.api_config = api_config

    def process_all_data(self):
        # Placeholder
        self.logger.log_info(f"📡 Processing data for Tautulli instance '{self.name}'...")
