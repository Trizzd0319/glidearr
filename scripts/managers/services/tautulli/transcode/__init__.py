from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.machine_learning.quality_analytics.transcode import (
    device_codec_matrix,
    transcode_stats,
)
from scripts.managers.machine_learning.quality_analytics.transcode_fingerprint import (
    serialize_fingerprint_matrix,
    transcode_fingerprint_matrix,
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

    def get_transcode_fingerprint_matrix(self, history_entries: list) -> list:
        """Per-device transcode CAPABILITY fingerprint — the (codec, audio, subtitle,
        res/HDR, location) matrix the Stage-C remote-play gate consumes. Returns the
        JSON-SAFE record list (serialize_fingerprint_matrix), not the raw tuple-keyed dict,
        because the cache stringifies tuple keys irreversibly; the consumer rebuilds the
        matrix with deserialize_fingerprint_matrix. Self-degrades to a codec-only read until
        the richer history fields are admitted to the projection. COMPUTATION lives in the
        brain (quality_analytics.transcode_fingerprint); manager keeps FETCH + log."""
        records = serialize_fingerprint_matrix(transcode_fingerprint_matrix(history_entries))
        self.logger.log_info(
            f"[TautulliTranscode] transcode-fingerprint matrix: {len(records)} cell(s)."
        )
        return records
