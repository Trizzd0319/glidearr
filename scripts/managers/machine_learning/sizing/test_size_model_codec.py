"""Tests for the codec-specific size dimension (A3): codec-qualified MiB/min keys
("quality@codec") size HEVC/VP9 files at their real rate, with a plain-quality
fallback so codec=None / no codec_col is byte-identical to the legacy behaviour.
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.machine_learning.sizing.size_model import (
    _norm_codec,
    estimate_gb,
    mb_per_min,
    measured_stats,
)

_MEAS = {"Bluray-1080p": 65.0, "Bluray-1080p@h265": 35.0}


def test_norm_codec_aliases():
    assert _norm_codec("x265") == "h265"
    assert _norm_codec("HEVC") == "h265"
    assert _norm_codec("h264") == "h264"
    assert _norm_codec("AVC") == "h264"
    assert _norm_codec("vp9") == "vp9"
    assert _norm_codec("") is None and _norm_codec(None) is None
    assert _norm_codec("weirdcodec") == "weirdcodec"   # unknown passes through


def test_mb_per_min_codec_qualified_then_fallback():
    assert mb_per_min("Bluray-1080p", _MEAS, codec="x265") == 35.0   # @h265 key (x265->h265)
    assert mb_per_min("Bluray-1080p", _MEAS, codec="h264") == 65.0   # no @h264 -> plain
    # codec=None / omitted -> plain (byte-identical to legacy)
    assert mb_per_min("Bluray-1080p", _MEAS) == 65.0
    assert mb_per_min("Bluray-1080p", _MEAS, codec=None) == 65.0


def test_estimate_gb_codec_aware():
    assert estimate_gb("Bluray-1080p", 100, 1, _MEAS, codec="x265") == 35.0 * 100 / 1024.0
    assert estimate_gb("Bluray-1080p", 100, 1, _MEAS) == 65.0 * 100 / 1024.0   # legacy


def _row(mbpm, codec):
    return {"size_bytes": mbpm * (1024 ** 2) * 100, "runtime_seconds": 100 * 60,
            "quality_name": "Bluray-1080p", "video_codec": codec}


def test_measured_stats_codec_keys_are_additive():
    df = pd.DataFrame([_row(65, "h264"), _row(35, "x265")])
    plain = measured_stats(df)                              # no codec_col
    withc = measured_stats(df, codec_col="video_codec")     # codec dimension
    # plain per-quality key is identical with/without codec_col (mean of both = 50, n=2)
    assert plain["Bluray-1080p"] == withc["Bluray-1080p"]
    assert plain["Bluray-1080p"]["n"] == 2
    # codec-qualified keys appear ONLY with codec_col
    assert "Bluray-1080p@h264" not in plain
    assert abs(withc["Bluray-1080p@h264"]["mean"] - 65.0) < 0.5 and withc["Bluray-1080p@h264"]["n"] == 1
    assert abs(withc["Bluray-1080p@h265"]["mean"] - 35.0) < 0.5 and withc["Bluray-1080p@h265"]["n"] == 1


def test_measured_stats_missing_codec_col_is_safe():
    df = pd.DataFrame([{"size_bytes": 65 * (1024 ** 2) * 100, "runtime_seconds": 6000,
                        "quality_name": "Bluray-1080p"}])   # no video_codec column
    out = measured_stats(df, codec_col="video_codec")       # missing col -> plain only, no crash
    assert "Bluray-1080p" in out and not any("@" in k for k in out)
