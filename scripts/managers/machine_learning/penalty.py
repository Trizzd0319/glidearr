from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit


class MLTranscodePenaltyScorer:
    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger, transcode_manager, config):
        self.logger = logger
        self.transcode = transcode_manager

    @LoggerManager().log_function_entry
    @timeit("get_transcode_penalty")
    def get_transcode_penalty(self, series_title, profile_name):
        """
        Calculates penalty score based on transcode mismatches.
        Could analyze:
        - Codec mismatch
        - Container incompatibility
        - Resolution mismatch
        """
        # Placeholder logic: default to 0 penalty
        self.logger.log_info(f"🔍 Calculating transcode penalty for {series_title} on profile {profile_name}")
        return 0
