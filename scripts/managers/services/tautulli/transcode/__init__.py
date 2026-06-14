from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.machine_learning.quality_analytics.transcode import (
    device_codec_matrix,
    transcode_stats,
)


class TautulliTranscodeManager(BaseManager):
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

    def get_transcode_stats(self, history_entries: list) -> dict:
        """Transcode stream-codec-pair tally. The COMPUTATION lives in the brain
        (quality_analytics.transcode.transcode_stats); the manager keeps the raw
        history FETCH + this summary log."""
        format_map = transcode_stats(history_entries)
        self.logger.log_info(
            f"[TautulliTranscode] {len(format_map)} transcode format combinations found."
        )
        return format_map

    def get_device_codec_matrix(self, history_entries: list) -> dict:
        """Per-device codec play-vs-transcode matrix (the keystone signal for
        per-device profile selection). COMPUTATION lives in the brain
        (quality_analytics.transcode.device_codec_matrix); manager keeps FETCH + log."""
        matrix = device_codec_matrix(history_entries)
        self.logger.log_info(
            f"[TautulliTranscode] device-codec matrix: {len(matrix)} device(s)."
        )
        return matrix
