from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.machine_learning.features.completion_stats import (
    series_completion_stats,
)


class TautulliSeriesManager(BaseManager):
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

    def get_series_completion_stats(self, history_entries: list) -> dict:
        """Per-show watched/incomplete episode counts. The COMPUTATION lives in the
        brain (features.completion_stats.series_completion_stats); the manager keeps
        the raw history FETCH + this summary log."""
        series_map = series_completion_stats(history_entries)
        self.logger.log_info(f"[TautulliSeries] {len(series_map)} shows tracked.")
        return series_map
