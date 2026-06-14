from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit


class MLUpgradeManager:
    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger, metadata_manager, transcode_manager):
        self.logger = logger
        self.metadata = metadata_manager
        self.transcode = transcode_manager

    @LoggerManager().log_function_entry
    @timeit("should_upgrade_episode")
    def should_upgrade_episode(self, series_title, episode_data):
        """
        Determines whether an episode should be upgraded/downgraded:
        - Compares current format vs user ideal
        - Checks space and playback trends
        """
        self.logger.log_info(f"🔍 Evaluating upgrade need for {series_title} {episode_data}")
        return False
