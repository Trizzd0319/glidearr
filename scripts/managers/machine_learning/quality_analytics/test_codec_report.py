"""Tests for quality_analytics.codec_report — the read-only codec-routing preview (pure)."""
from __future__ import annotations

import pandas as pd

from scripts.managers.machine_learning.quality_analytics.codec_report import (
    build_per_title_watchers,
    codec_report_rows,
    normalize_title,
    per_user_platform_usage_from_history,
)


def _prof(pid, name, res):
    return {"id": pid, "name": name,
            "items": [{"allowed": True, "quality": {"resolution": res, "name": f"q{res}"}}]}


HEVC = _prof(16, "WEB-1080p (HEVC)", 1080)
H264 = _prof(15, "WEB-1080p (H264)", 1080)
AV1 = _prof(17, "WEB-1080p (AV1)", 1080)
PROFS = [HEVC, H264, AV1]


def _cell(direct, transcode):
    return {"direct": direct, "transcode": transcode, "n": direct + transcode, "last_seen": 0}


# userA on a PS5: direct-plays HEVC, transcodes AV1.
MATRIX = {"A": {
    ("PS5", ("hevc", "unknown", "none", "1080p_sdr", "unknown")): _cell(10, 0),
    ("PS5", ("av1", "unknown", "none", "1080p_sdr", "unknown")): _cell(0, 10),
}}
PLATFORM = {"A": {"PS5": 1}}


# ── assembly helpers ────────────────────────────────────────────────────────────────
def test_build_per_title_watchers_groups_by_title_and_user():
    hist = [
        {"title": "The Bear", "user": "A"}, {"title": "The Bear", "user": "A"},
        {"title": "the bear", "user": "B"},                       # case-normalised join
        {"grandparent_title": "Severance", "user_id": "9"},       # episode title fallback
    ]
    assert build_per_title_watchers(hist) == {"the bear": {"A": 2, "B": 1}, "severance": {"9": 1}}


def test_per_user_platform_usage_from_history():
    hist = [{"user": "A", "platform": "PS5"}, {"user": "A", "platform": "PS5"},
            {"user": "B", "platform": "WebOS"}]
    assert per_user_platform_usage_from_history(hist) == {"A": {"PS5": 2}, "B": {"WebOS": 1}}


def test_normalize_title():
    assert normalize_title("  The   Bear ") == "the bear"


# ── the report ──────────────────────────────────────────────────────────────────────
def test_report_flags_transcoding_title_and_skips_optimal_and_unwatched():
    watchers = {"the bear": {"A": 5}, "severance": {"A": 5}}
    df = pd.DataFrame([
        {"title": "The Bear", "video_codec": "av1", "resolution": 1080},    # transcodes -> recommend HEVC
        {"title": "Severance", "video_codec": "x265", "resolution": 1080},  # x265==hevc -> already optimal
        {"title": "Unwatched", "video_codec": "av1", "resolution": 1080},   # no history -> skipped
    ])
    rows = codec_report_rows(df, PROFS, MATRIX, PLATFORM, watchers)
    by_title = {r["title"]: r for r in rows}
    assert set(by_title) == {"The Bear", "Severance"}                       # unwatched dropped
    bear = by_title["The Bear"]
    assert bear["current_codec"] == "av1" and bear["recommended_codec"] == "hevc"
    assert bear["current_cost"] == 1.0 and bear["recommended_cost"] == 0.0  # av1 transcodes, hevc direct
    assert bear["change"] is True                                          # real reduction -> flagged
    sev = by_title["Severance"]
    assert sev["current_codec"] == "hevc"                                  # x265 normalised to hevc
    assert sev["current_cost"] == sev["recommended_cost"] and sev["change"] is False  # already optimal
    assert rows[0]["title"] == "The Bear"                                   # change-first sort


def test_no_transcode_signal_does_not_flag_a_change():
    # The 40/40 guard: with an EMPTY matrix every candidate ties at the neutral prior, so the size
    # tie-break recommends AV1 — but current_cost == recommended_cost (both none_p), so gain is 0 and
    # it is NOT flagged as a change. A recommendation needs real transcode evidence to count.
    df = pd.DataFrame([{"title": "Cold Movie", "video_codec": "hevc", "resolution": 1080}])
    rows = codec_report_rows(df, PROFS, {}, {}, {"cold movie": {"Z": 5}})  # user Z has no matrix data
    assert len(rows) == 1
    r = rows[0]
    assert r["recommended_codec"] == "av1"                                 # cold size default
    assert r["current_cost"] == r["recommended_cost"]                      # both the neutral prior
    assert r["change"] is False                                           # no evidence -> no change


def test_report_skips_tier_with_fewer_than_two_variants():
    df = pd.DataFrame([{"title": "X", "video_codec": "av1", "resolution": 2160}])
    rows = codec_report_rows(df, [_prof(5, "Ultra-HD", 2160)], MATRIX, PLATFORM, {"x": {"A": 5}})
    assert rows == []


def test_report_empty_when_no_watch_history():
    df = pd.DataFrame([{"title": "The Bear", "video_codec": "av1", "resolution": 1080}])
    assert codec_report_rows(df, PROFS, MATRIX, PLATFORM, {}) == []
