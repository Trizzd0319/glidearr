"""Tests for quality_analytics.transcode — the transcode usage derivations, incl.
the per-device codec play/transcode matrix (A2) that disambiguates "direct-played"
from "never tried"."""
from __future__ import annotations

from scripts.managers.machine_learning.quality_analytics.transcode import (
    codec_direct_play_rate,
    device_codec_matrix,
    transcode_stats,
)

_HIST = [
    {"platform": "Shield", "stream_video_codec": "hevc", "stream_audio_codec": "eac3", "transcode_decision": "direct play"},
    {"platform": "Shield", "stream_video_codec": "hevc", "stream_audio_codec": "ac3",  "transcode_decision": "transcode"},
    {"platform": "Shield", "stream_video_codec": "hevc", "stream_audio_codec": "eac3", "transcode_decision": "direct play"},
    {"platform": "iPhone", "stream_video_codec": "hevc", "stream_audio_codec": "aac",  "transcode_decision": "transcode"},
    {"platform": "iPhone", "stream_video_codec": "h264", "stream_audio_codec": "aac",  "transcode_decision": "direct play"},
    {"platform": "Web",    "stream_video_codec": "h264", "stream_audio_codec": "aac",  "transcode_decision": ""},  # no signal -> skipped
]


def test_device_codec_matrix_counts_direct_and_transcode_per_device():
    m = device_codec_matrix(_HIST)
    assert m["Shield"]["hevc/eac3"] == {"direct": 2, "transcode": 0}
    assert m["Shield"]["hevc/ac3"] == {"direct": 0, "transcode": 1}
    assert m["iPhone"]["hevc/aac"] == {"direct": 0, "transcode": 1}
    assert m["iPhone"]["h264/aac"] == {"direct": 1, "transcode": 0}
    assert "Web" not in m   # empty decision contributes no signal


def test_device_codec_matrix_defaults_unknown():
    m = device_codec_matrix([{"transcode_decision": "transcode"}])
    assert m["unknown"]["unknown/unknown"] == {"direct": 0, "transcode": 1}


def test_codec_direct_play_rate_aggregates_and_handles_no_sample():
    m = device_codec_matrix(_HIST)
    # Shield hevc: 2 direct (eac3) + 1 transcode (ac3) -> 2/3
    assert abs(codec_direct_play_rate(m, "Shield", "hevc") - 2 / 3) < 1e-9
    # iPhone hevc: 0 direct, 1 transcode -> 0.0 (always transcodes)
    assert codec_direct_play_rate(m, "iPhone", "hevc") == 0.0
    # iPhone h264: always direct -> 1.0
    assert codec_direct_play_rate(m, "iPhone", "h264") == 1.0
    # never tried -> None (distinct from 0.0)
    assert codec_direct_play_rate(m, "Shield", "av1") is None
    assert codec_direct_play_rate(m, "NoSuchDevice", "h264") is None


def test_transcode_stats_unchanged():
    # the legacy event-only tally still works (only transcodes, no device, no direct)
    assert transcode_stats(_HIST) == {"hevc/ac3": 1, "hevc/aac": 1}
