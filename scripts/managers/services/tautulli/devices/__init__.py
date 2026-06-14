from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.machine_learning.affinity.platform_usage import platform_usage


class TautulliDevicesManager(BaseManager):
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

    def get_platform_usage(self, history_entries: list) -> dict:
        """Platform play-count tally. The COMPUTATION lives in the brain
        (affinity.platform_usage.platform_usage); the manager keeps the raw history
        FETCH + this summary log."""
        result = platform_usage(history_entries)
        self.logger.log_info(f"[TautulliDevices] {len(result)} platforms found.")
        return result
