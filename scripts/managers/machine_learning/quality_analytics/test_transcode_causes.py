"""Tests for quality_analytics.transcode_causes — per-viewer transcode-cause decomposition (pure)."""
from __future__ import annotations

from scripts.managers.machine_learning.quality_analytics.transcode_causes import (
    extract_stream_decision,
    transcode_cause_breakdown,
)


def _e(user, decision, *, row_id=None, vdec=None, adec=None, sub=None, loc="lan", streamed="h264", rk=None):
    e = {"user": user, "transcode_decision": decision, "stream_video_codec": streamed, "location": loc}
    if row_id is not None: e["row_id"] = row_id
    if vdec is not None: e["video_decision"] = vdec
    if adec is not None: e["audio_decision"] = adec
    if sub is not None: e["subtitle_decision"] = sub
    if rk is not None: e["rating_key"] = rk
    return e


def test_extract_stream_decision():
    resp = {"response": {"data": {"stream_video_decision": "copy", "audio_decision": "transcode",
                                  "stream_subtitle_decision": "burn", "stream_container_decision": "copy",
                                  "video_codec": "hevc", "stream_video_codec": "hevc"}}}
    sd = extract_stream_decision(resp)
    assert sd == {"video_decision": "copy", "audio_decision": "transcode", "subtitle_decision": "burn",
                  "container_decision": "copy", "video_codec": "hevc", "stream_video_codec": "hevc"}
    assert extract_stream_decision({}) == {"video_decision": None, "audio_decision": None,
                                           "subtitle_decision": None, "container_decision": None,
                                           "video_codec": None, "stream_video_codec": None}


def test_breakdown_from_stream_decisions_map():
    # Decisions come from the per-row stream_decisions map (the real wiring), keyed by row_id.
    hist = [_e("A", "transcode", row_id="100"), _e("A", "transcode", row_id="101"), _e("A", "direct play")]
    sd_map = {
        "100": {"video_decision": "transcode", "video_codec": "hevc", "stream_video_codec": "h264"},  # codec
        "101": {"audio_decision": "transcode"},                                                        # audio
    }
    out = transcode_cause_breakdown(hist, sd_map)["A"]
    assert out["causes"] == {"video: codec": 1, "audio": 1}
    assert out["transcodes"] == 2 and out["directs"] == 1 and out["ground_truth"] is True


def test_video_codec_vs_bitrate_from_decisions():
    sd1 = {"1": {"video_decision": "transcode", "video_codec": "hevc", "stream_video_codec": "h264"}}
    assert transcode_cause_breakdown([_e("A", "transcode", row_id="1")], sd1)["A"]["causes"] == {"video: codec": 1}
    sd2 = {"1": {"video_decision": "transcode", "video_codec": "h264", "stream_video_codec": "h264"}}
    assert transcode_cause_breakdown([_e("A", "transcode", row_id="1")], sd2)["A"]["causes"] == {"video: bitrate/res": 1}


def test_subtitle_and_container():
    sd = {"1": {"subtitle_decision": "burn"}, "2": {"container_decision": "transcode"}}
    out = transcode_cause_breakdown([_e("A", "transcode", row_id="1"), _e("A", "transcode", row_id="2")], sd)["A"]
    assert out["causes"] == {"subtitle": 1, "container": 1}


def test_entry_decisions_when_no_map():
    # No stream_decisions map -> falls back to decisions on the entry itself (still ground truth).
    out = transcode_cause_breakdown([_e("A", "transcode", adec="transcode")], None)["A"]
    assert out["causes"] == {"audio": 1} and out["ground_truth"] is True


def test_heuristic_fallback_without_any_decisions():
    # No decisions anywhere -> heuristic from metadata source(hevc) vs streamed(h264).
    out = transcode_cause_breakdown([_e("A", "transcode", rk="1", streamed="h264")], {}, {"1": {"video_codec": "hevc"}})
    assert out["A"]["causes"] == {"video: codec": 1} and out["A"]["ground_truth"] is False


def test_rate_and_sorting():
    sd = {str(i): {"video_decision": "transcode", "video_codec": "hevc", "stream_video_codec": "h264"} for i in range(3)}
    hist = [_e("A", "transcode", row_id=str(i)) for i in range(3)] + [_e("A", "direct play")] * 7
    out = transcode_cause_breakdown(hist, sd)["A"]
    assert out["transcodes"] == 3 and out["directs"] == 7 and out["rate"] == 0.3
    assert list(out["causes"]) == ["video: codec"]


def test_skips_empty_decision():
    assert transcode_cause_breakdown([{"user": "A", "transcode_decision": ""}]) == {}
