"""The cached history projection must capture the unix `date` (drives temporal
affinity decay) and the transcode-capability fingerprint axes (Stage-C), while still
dropping the identifying PII (ip/machine/friendly_name)."""
from __future__ import annotations

from scripts.managers.services.tautulli.watch_history import (
    TautulliWatchHistoryManager,
    _CACHED_HISTORY_FIELDS,
)


def test_date_is_captured_pii_is_dropped():
    assert "date" in _CACHED_HISTORY_FIELDS
    raw = {
        "rating_key": "55", "user_id": 7, "date": 1749513600, "percent_complete": 95,
        "ip_address": "192.168.1.5", "friendly_name": "Rob", "machine_id": "abc",
    }
    proj = TautulliWatchHistoryManager._project_record(raw)
    assert proj["date"] == 1749513600
    assert proj["rating_key"] == "55" and proj["user_id"] == 7
    assert "ip_address" not in proj and "friendly_name" not in proj and "machine_id" not in proj


def test_transcode_fingerprint_fields_are_captured():
    # The 3 axes the capability fingerprint needs (Stage C) are now projected…
    for f in ("subtitle_decision", "stream_video_full_resolution", "location"):
        assert f in _CACHED_HISTORY_FIELDS
    raw = {
        "rating_key": "9", "user": "alice", "platform": "Chromecast",
        "transcode_decision": "transcode", "stream_video_codec": "hevc",
        "stream_audio_codec": "eac3", "subtitle_decision": "burn",
        "stream_video_full_resolution": "4k", "location": "wan",
        # …but the identifying PII alongside them stays dropped.
        "ip_address": "8.8.8.8", "machine_id": "dev-xyz",
    }
    proj = TautulliWatchHistoryManager._project_record(raw)
    assert proj["subtitle_decision"] == "burn"
    assert proj["stream_video_full_resolution"] == "4k"
    assert proj["location"] == "wan"                       # the lan/wan bit, not an IP
    assert "ip_address" not in proj and "machine_id" not in proj
