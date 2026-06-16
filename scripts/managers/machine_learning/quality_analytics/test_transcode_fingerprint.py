"""Tests for the per-device transcode capability matrix (transcode_fingerprint).

Covers axis normalisation, the matrix/per-user builders, the candidate source
fingerprint, the graded-fallback predictor, and the bounded explore/exploit tier
decision. All pure — synthetic history rows, no I/O."""
from __future__ import annotations

import json

from scripts.managers.machine_learning.quality_analytics.transcode_fingerprint import (
    _norm_video, _norm_audio, _norm_subtitle, _norm_res_hdr, _norm_location,
    row_fingerprint, source_fingerprint,
    transcode_fingerprint_matrix, per_user_transcode_fingerprint_matrix,
    serialize_fingerprint_matrix, deserialize_fingerprint_matrix,
    predict_transcode, choose_tier, can_remote_play,
)


def _row(platform="Chromecast", vc="hevc", ac="eac3", sub="none", res="4k",
         loc="lan", decision="transcode", date=1000, user="alice"):
    return {"platform": platform, "stream_video_codec": vc, "stream_audio_codec": ac,
            "subtitle_decision": sub, "stream_video_full_resolution": res, "location": loc,
            "transcode_decision": decision, "date": date, "user": user}


# ── normalisers ───────────────────────────────────────────────────────────────
def test_video_aliases():
    assert _norm_video("h265") == "hevc" and _norm_video("x265") == "hevc"
    assert _norm_video("H264") == "h264" and _norm_video("AVC1") == "h264"
    assert _norm_video("av1") == "av1" and _norm_video("") == "unknown"


def test_res_hdr_buckets():
    assert _norm_res_hdr("4k hdr") == "2160p_hdr"
    assert _norm_res_hdr("4K") == "2160p_sdr"
    assert _norm_res_hdr("1080") == "1080p_sdr"
    assert _norm_res_hdr("720") == "720p"
    assert _norm_res_hdr("") == "unknown"


def test_subtitle_and_location():
    assert _norm_subtitle("") == "none" and _norm_subtitle("transcode") == "burn"
    assert _norm_subtitle("copy") == "copy"
    assert _norm_location("WAN") == "wan" and _norm_location("lan") == "lan"
    assert _norm_location("somewhere") == "unknown"
    assert _norm_audio("EAC3") == "eac3"


# ── matrix builders ───────────────────────────────────────────────────────────
def test_matrix_counts_direct_transcode_and_lastseen():
    hist = [_row(decision="transcode", date=10), _row(decision="transcode", date=30),
            _row(decision="direct play", date=20)]
    m = transcode_fingerprint_matrix(hist)
    key = ("Chromecast", ("hevc", "eac3", "none", "2160p_sdr", "lan"))
    assert m[key] == {"direct": 1, "transcode": 2, "last_seen": 30, "n": 3}


def test_matrix_skips_empty_decision_and_splits_devices():
    hist = [_row(decision=""), _row(platform="PS5", decision="direct play")]
    m = transcode_fingerprint_matrix(hist)
    assert len(m) == 1                                    # the empty-decision row is dropped
    assert ("PS5", ("hevc", "eac3", "none", "2160p_sdr", "lan")) in m


def test_per_user_matrix_groups_by_user():
    hist = [_row(user="alice"), _row(user="bob", decision="direct play")]
    pum = per_user_transcode_fingerprint_matrix(hist)
    assert set(pum) == {"alice", "bob"}
    assert sum(b["transcode"] for b in pum["alice"].values()) == 1
    assert sum(b["direct"] for b in pum["bob"].values()) == 1


# ── JSON round-trip (cache serialization) ─────────────────────────────────────
def test_serialize_deserialize_round_trip_is_identity():
    hist = [_row(decision="transcode", date=10), _row(decision="direct play", date=20),
            _row(platform="PS5", vc="h264", res="1080", decision="direct play")]
    matrix = transcode_fingerprint_matrix(hist)
    records = serialize_fingerprint_matrix(matrix)
    # The serialized form survives a real JSON dump/load (the cache layer's transport)…
    revived = deserialize_fingerprint_matrix(json.loads(json.dumps(records)))
    assert revived == matrix                                   # …and rebuilds the exact matrix
    # the predictor reads the revived matrix identically to the original
    assert predict_transcode(matrix, ("hevc", "eac3", "none", "2160p_sdr", "lan"), {"Chromecast": 1}) == \
           predict_transcode(revived, ("hevc", "eac3", "none", "2160p_sdr", "lan"), {"Chromecast": 1})


def test_serialize_empty_matrix():
    assert serialize_fingerprint_matrix({}) == []
    assert serialize_fingerprint_matrix(None) == []
    assert deserialize_fingerprint_matrix([]) == {}
    assert deserialize_fingerprint_matrix(None) == {}


def test_deserialize_skips_malformed_records():
    records = [
        {"device": "PS5", "fingerprint": ["h264", "ac3", "none", "1080p_sdr", "lan"],
         "direct": 4, "transcode": 0, "last_seen": 5, "n": 4},
        "not-a-dict",                                          # skipped
        {"fingerprint": ["x"]},                                # missing device → skipped
        {"device": "TV", "fingerprint": "oops"},               # fingerprint not a list → skipped
    ]
    out = deserialize_fingerprint_matrix(records)
    assert list(out) == [("PS5", ("h264", "ac3", "none", "1080p_sdr", "lan"))]
    assert out[("PS5", ("h264", "ac3", "none", "1080p_sdr", "lan"))]["direct"] == 4


# ── source fingerprint (candidate file) ───────────────────────────────────────
def test_source_fingerprint_from_mediainfo():
    fp = source_fingerprint(video_codec="HEVC", audio_codec="TrueHD", subtitles="eng",
                            height=2160, hdr="HDR10", location="wan")
    assert fp == ("hevc", "truehd", "copy", "2160p_hdr", "wan")
    # no subs + sdr 1080p
    assert source_fingerprint(video_codec="h264", height=1080)[2:4] == ("none", "1080p_sdr")


# ── predictor ─────────────────────────────────────────────────────────────────
def test_predict_exact_cell():
    hist = [_row(decision="transcode") for _ in range(7)]
    m = transcode_fingerprint_matrix(hist)
    fp = ("hevc", "eac3", "none", "2160p_sdr", "lan")
    p, level = predict_transcode(m, fp, {"Chromecast": 1.0}, min_n=3)
    assert p == 1.0 and level == "exact"


def test_predict_graded_fallback_drops_axes():
    # exact (hevc,eac3,none,...) has no cell; drop_audio aggregates a burn transcode +
    # 3 direct plays at the same device/res/loc → P = 1/4.
    m = {
        ("Chromecast", ("hevc", "eac3", "burn", "2160p_hdr", "lan")): {"transcode": 1, "direct": 0, "n": 1, "last_seen": 0},
        ("Chromecast", ("hevc", "truehd", "none", "2160p_hdr", "lan")): {"transcode": 0, "direct": 3, "n": 3, "last_seen": 0},
    }
    fp = ("hevc", "eac3", "none", "2160p_hdr", "lan")
    p, level = predict_transcode(m, fp, {"Chromecast": 1.0}, min_n=3)
    assert level == "drop_audio" and abs(p - 0.25) < 1e-9


def test_predict_play_share_weighted_across_devices():
    m = {
        ("Chromecast", ("hevc", "eac3", "none", "2160p_hdr", "lan")): {"transcode": 5, "direct": 0, "n": 5, "last_seen": 0},
        ("AppleTV",    ("hevc", "eac3", "none", "2160p_hdr", "lan")): {"transcode": 0, "direct": 5, "n": 5, "last_seen": 0},
    }
    fp = ("hevc", "eac3", "none", "2160p_hdr", "lan")
    p, _ = predict_transcode(m, fp, {"Chromecast": 0.7, "AppleTV": 0.3}, min_n=3)
    assert abs(p - 0.7) < 1e-9            # weighted toward the device they mostly use


def test_predict_none_without_evidence():
    p, level = predict_transcode({}, ("av1", "opus", "none", "2160p_hdr", "wan"), {"Roku": 1.0})
    assert p is None and level == "no_data"


def test_predict_household_fallback_when_device_unknown():
    # no cell for the user's device, but the household has 3 plays of this codec+res
    m = {("OtherTV", ("hevc", "eac3", "none", "2160p_hdr", "lan")): {"transcode": 3, "direct": 0, "n": 3, "last_seen": 0}}
    p, level = predict_transcode(m, ("hevc", "eac3", "none", "2160p_hdr", "wan"), {"Roku": 1.0}, min_n=3)
    assert p == 1.0 and level == "hh_codec_res"


# ── explore / exploit tier decision ───────────────────────────────────────────
def test_choose_tier_exploits_hd_when_transcode_likely():
    m = {("Chromecast", ("hevc", "eac3", "none", "2160p_hdr", "wan")): {"transcode": 4, "direct": 0, "n": 4, "last_seen": 0}}
    tier, reason = choose_tier(m, ("hevc", "eac3", "none", "2160p_hdr", "wan"), {"Chromecast": 1.0})
    assert tier == "hd" and "exploit" in reason


def test_choose_tier_keeps_4k_when_direct_play_likely():
    m = {("PS5", ("hevc", "truehd", "none", "2160p_hdr", "lan")): {"transcode": 0, "direct": 4, "n": 4, "last_seen": 0}}
    tier, _ = choose_tier(m, ("hevc", "truehd", "none", "2160p_hdr", "lan"), {"PS5": 1.0})
    assert tier == "4k"


def test_choose_tier_explores_4k_when_no_data():
    tier, reason = choose_tier({}, ("av1", "opus", "none", "2160p_hdr", "wan"), {"Roku": 1.0})
    assert tier == "4k" and "explore" in reason


def test_choose_tier_exploits_thin_evidence_past_explore_cap():
    # exact cell has n=2 (>= explore_cap) but < min_n=3 → no trusted read, exploit on it
    fp = ("hevc", "eac3", "none", "2160p_hdr", "wan")
    m = {("Roku", fp): {"transcode": 2, "direct": 0, "n": 2, "last_seen": 0}}
    tier, reason = choose_tier(m, fp, {"Roku": 1.0}, min_n=3, explore_cap=2)
    assert tier == "hd" and "thin" in reason


# ── can_remote_play (the Stage-C single-authority boolean) ────────────────────
def test_can_remote_play_false_when_transcode_likely():
    fp = ("hevc", "eac3", "none", "2160p_hdr", "wan")
    m = {("Chromecast", fp): {"transcode": 4, "direct": 0, "n": 4, "last_seen": 0}}
    assert can_remote_play(m, fp, {"Chromecast": 1.0}) is False     # would transcode → no 4K


def test_can_remote_play_true_when_direct_play_likely():
    fp = ("hevc", "truehd", "none", "2160p_hdr", "lan")
    m = {("PS5", fp): {"transcode": 0, "direct": 4, "n": 4, "last_seen": 0}}
    assert can_remote_play(m, fp, {"PS5": 1.0}) is True              # direct-plays → 4K worth it


def test_can_remote_play_true_on_no_data_explores():
    # cold/empty matrix → explore (acquire 4K, learn) — a fresh household is never denied
    assert can_remote_play({}, ("av1", "opus", "none", "2160p_hdr", "wan"), {"Roku": 1.0}) is True
    assert can_remote_play({}, ("hevc", "eac3", "none", "2160p_hdr", "lan"), {}) is True


def test_can_remote_play_matches_choose_tier_exactly():
    # the wrapper must agree with choose_tier on every read (single authority, no second policy)
    fp = ("hevc", "eac3", "none", "2160p_hdr", "wan")
    for buckets in ({"transcode": 4, "direct": 0, "n": 4, "last_seen": 0},
                    {"transcode": 0, "direct": 4, "n": 4, "last_seen": 0}):
        m = {("Roku", fp): buckets}
        tier, _ = choose_tier(m, fp, {"Roku": 1.0})
        assert can_remote_play(m, fp, {"Roku": 1.0}) is (tier == "4k")
