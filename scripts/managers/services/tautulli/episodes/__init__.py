from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.machine_learning.features.completion_stats import (
    episode_completion_stats,
)


class TautulliEpisodesManager(BaseManager):
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

    def get_episode_completion_stats(self, history_entries: list) -> dict:
        """Household-wide episode completion tally. The COMPUTATION lives in the brain
        (features.completion_stats.episode_completion_stats); the manager keeps the
        raw history FETCH + this summary log."""
        stats = episode_completion_stats(history_entries)
        self.logger.log_info(
            f"[TautulliEpisodes] {stats['completed']} complete / {stats['incomplete']} incomplete."
        )
        return stats
