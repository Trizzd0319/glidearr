"""Tests for quality_analytics.transcode_causes — per-viewer transcode-cause decomposition (pure)."""
from __future__ import annotations

from scripts.managers.machine_learning.quality_analytics.transcode_causes import (
    transcode_cause_breakdown,
)


def _e(user, decision, *, vdec=None, adec=None, sub=None, loc="lan", streamed="h264", rk=None):
    e = {"user": user, "transcode_decision": decision, "stream_video_codec": streamed, "location": loc}
    if vdec is not None: e["video_decision"] = vdec
    if adec is not None: e["audio_decision"] = adec
    if sub is not None: e["subtitle_decision"] = sub
    if rk is not None: e["rating_key"] = rk
    return e


def test_ground_truth_video_codec_vs_bitrate():
    # video transcoded + source(hevc) != streamed target(h264) -> codec cause.
    out = transcode_cause_breakdown(
        [_e("A", "transcode", vdec="transcode", streamed="h264", rk="1")], {"1": {"video_codec": "hevc"}})
    assert out["A"]["causes"] == {"video: codec": 1} and out["A"]["ground_truth"] is True
    # video transcoded but source codec == streamed codec -> a bitrate/resolution downscale, not codec.
    out2 = transcode_cause_breakdown(
        [_e("A", "transcode", vdec="transcode", streamed="h264", rk="1")], {"1": {"video_codec": "h264"}})
    assert out2["A"]["causes"] == {"video: bitrate/res": 1}


def test_ground_truth_audio_and_subtitle():
    out = transcode_cause_breakdown(
        [_e("A", "transcode", vdec="copy", adec="transcode"), _e("A", "transcode", sub="burn")], {})
    assert out["A"]["causes"] == {"audio": 1, "subtitle": 1} and out["A"]["ground_truth"] is True


def test_rate_directs_and_sorting():
    hist = ([_e("A", "transcode", vdec="transcode", streamed="h264", rk="1")] * 3 +   # video: codec x3
            [_e("A", "transcode", adec="transcode")] +                                # audio x1
            [_e("A", "direct play")] * 6)
    out = transcode_cause_breakdown(hist, {"1": {"video_codec": "hevc"}})["A"]
    assert out["transcodes"] == 4 and out["directs"] == 6 and out["rate"] == 0.4
    assert list(out["causes"]) == ["video: codec", "audio"]                          # most-common first


def test_heuristic_fallback_without_decisions():
    # No per-stream decisions -> heuristic: source(hevc) != streamed(h264) -> codec, ground_truth False.
    out = transcode_cause_breakdown([_e("A", "transcode", streamed="h264", rk="1")], {"1": {"video_codec": "hevc"}})
    assert out["A"]["causes"] == {"video: codec": 1} and out["A"]["ground_truth"] is False
    # No decisions + remote -> bandwidth.
    out2 = transcode_cause_breakdown([_e("B", "transcode", loc="wan", streamed="h264")], {})
    assert out2["B"]["causes"] == {"remote (bandwidth)": 1}


def test_skips_empty_decision():
    assert transcode_cause_breakdown([{"user": "A", "transcode_decision": ""}], {}) == {}
