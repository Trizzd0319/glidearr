from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.machine_learning.quality_analytics.transcode import (
    device_codec_matrix,
    transcode_stats,
)
from scripts.managers.machine_learning.quality_analytics.transcode_causes import (
    extract_stream_decision,
)
from scripts.managers.machine_learning.quality_analytics.transcode_fingerprint import (
    serialize_fingerprint_matrix,
    transcode_fingerprint_matrix,
)

# global_cache key for the per-row stream-decision detail (immutable per play).
STREAM_DECISIONS_KEY = "tautulli/stream_decisions"


class TautulliTranscodeManager(BaseManager):
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.tautulli_api = kwargs.get("tautulli_api")

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

    def get_stream_decisions_cached(self, history_entries: list) -> dict:
        """``{row_id(str): per-stream decisions}`` for every TRANSCODED history row — the per-stream
        detail (video/audio/subtitle/container decision + source-vs-stream codec) that ``get_history``
        omits, fetched from ``get_stream_data`` and CACHED per row_id. A play's stream data is
        immutable, so this is incremental: only row_ids not already cached are fetched (one API call
        each). Feeds the per-viewer transcode-cause breakdown. Returns the full cached map."""
        cache_key = STREAM_DECISIONS_KEY
        cached = dict(self.global_cache.get(cache_key) or {}) if self.global_cache else {}
        if not self.tautulli_api:
            return cached
        want = []
        for entry in (history_entries or []):
            if str(entry.get("transcode_decision") or "").strip().lower() != "transcode":
                continue
            rid = str(entry.get("row_id") or entry.get("reference_id") or entry.get("id") or "")
            if rid and rid not in cached:
                want.append(rid)
        fetched = 0
        for rid in want:
            try:
                cached[rid] = extract_stream_decision(self.tautulli_api.get_stream_data(rid))
            except Exception:
                cached[rid] = {}
            fetched += 1
        if fetched and self.global_cache:
            try:
                self.global_cache.set(cache_key, cached)
            except Exception:
                pass
        self.logger.log_info(
            f"[TautulliTranscode] stream decisions: {fetched} newly fetched, {len(cached)} cached "
            f"(per-stream transcode causes)."
        )
        return cached
