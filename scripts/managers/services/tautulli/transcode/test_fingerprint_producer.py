"""C1: the TautulliTranscodeManager.get_transcode_fingerprint_matrix producer returns the
JSON-SAFE serialized record list (not the raw tuple-keyed matrix), so the orchestration can
cache it under tautulli/transcode_fingerprint and a consumer can rebuild it. A stub manager
(object.__new__) bypasses the heavy __init__."""
from __future__ import annotations

from scripts.managers.machine_learning.quality_analytics.transcode_fingerprint import (
    deserialize_fingerprint_matrix,
    transcode_fingerprint_matrix,
)
from scripts.managers.services.tautulli.transcode import TautulliTranscodeManager


class _Log:
    def log_info(self, m): pass
    def log_warning(self, m): pass
    def log_debug(self, m): pass
    def log_error(self, m): pass


def _mgr():
    m = object.__new__(TautulliTranscodeManager)
    m.logger = _Log()
    return m


def _row(**kw):
    base = {"platform": "Chromecast", "stream_video_codec": "hevc", "stream_audio_codec": "eac3",
            "subtitle_decision": "none", "stream_video_full_resolution": "4k", "location": "lan",
            "transcode_decision": "transcode", "date": 1000, "user": "alice"}
    base.update(kw)
    return base


def test_producer_returns_serialized_records():
    hist = [_row(decision="transcode"), _row(transcode_decision="direct play")]
    records = _mgr().get_transcode_fingerprint_matrix(hist)
    # it is the JSON-safe list shape, not the tuple-keyed dict
    assert isinstance(records, list)
    assert all(isinstance(r, dict) and "device" in r and "fingerprint" in r for r in records)
    # and it round-trips back to exactly the pure matrix
    assert deserialize_fingerprint_matrix(records) == transcode_fingerprint_matrix(hist)


def test_producer_empty_history():
    assert _mgr().get_transcode_fingerprint_matrix([]) == []
