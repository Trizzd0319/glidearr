"""Tests for quality_analytics.profile_selector — the codec-aware profile-selection brain.

Pure: synthetic per-user transcode matrices + minimal profile dicts, no I/O. Covers the
coverage-max / accept-minority objective, the size tie-break, cold-data default, the
profile→codec classifier (name + CF reconciliation incl. bans), and the fingerprint delegation.
"""
from __future__ import annotations

from scripts.managers.machine_learning.quality_analytics.profile_selector import (
    candidate_fingerprint,
    choose_codec_profile,
    classify_profile_axes,
    viewer_transcode_cost,
)
from scripts.managers.machine_learning.quality_analytics.transcode_fingerprint import (
    source_fingerprint,
)


def _prof(pid, name, res, cf_scores=None):
    p = {"id": pid, "name": name,
         "items": [{"allowed": True, "quality": {"resolution": res, "name": f"q{res}"}}]}
    if cf_scores is not None:
        p["cf_scores"] = cf_scores
    return p


HEVC = _prof(16, "WEB-1080p (HEVC)", 1080)
H264 = _prof(15, "WEB-1080p (H264)", 1080)
AV1 = _prof(17, "WEB-1080p (AV1)", 1080)
PROFS = [HEVC, H264, AV1]


def _matrix(*cells):
    """Build {(device, fingerprint): bucket} from (device, codec, direct, transcode) tuples,
    using the 1080p_sdr fingerprint shape candidate_fingerprint produces."""
    out = {}
    for device, codec, direct, transcode in cells:
        fp = (codec, "unknown", "none", "1080p_sdr", "unknown")
        out[(device, fp)] = {"direct": direct, "transcode": transcode,
                             "n": direct + transcode, "last_seen": 0}
    return out


# ── the objective ─────────────────────────────────────────────────────────────────
def test_single_viewer_picks_their_directplay_codec():
    # One viewer on a PS5: direct-plays HEVC (0/10 transcode), transcodes AV1, mixed on H264.
    matrix = {"A": _matrix(("PS5", "hevc", 10, 0), ("PS5", "av1", 0, 10), ("PS5", "h264", 5, 5))}
    pid, reason = choose_codec_profile(
        1080, {"A": 1.0}, matrix, PROFS, per_user_platform_weights={"A": {"PS5": 1.0}})
    assert pid == 16 and reason["codec"] == "hevc" and reason["cost"] == 0.0


def test_shared_title_coverage_max_accepts_minority():
    # A (PS5) direct-plays HEVC+H264, transcodes AV1; B (an AV1-only box) direct-plays AV1 only.
    # No codec direct-plays for BOTH, so the majority watch-share (A=0.7) wins and B transcodes.
    matrix = {
        "A": _matrix(("PS5", "hevc", 10, 0), ("PS5", "h264", 10, 0), ("PS5", "av1", 0, 10)),
        "B": _matrix(("Box", "av1", 10, 0), ("Box", "hevc", 0, 10), ("Box", "h264", 0, 10)),
    }
    weights = {"A": {"PS5": 1.0}, "B": {"Box": 1.0}}
    pid, reason = choose_codec_profile(
        1080, {"A": 0.7, "B": 0.3}, matrix, PROFS, per_user_platform_weights=weights)
    # HEVC: 0.7*0 + 0.3*1 = 0.3 ; H264: 0.3 ; AV1: 0.7. Tie HEVC/H264 -> size picks HEVC.
    assert pid == 16 and reason["codec"] == "hevc" and reason["cost"] == 0.3


def test_size_tiebreak_prefers_efficient_codec_when_costs_tie():
    # A device that direct-plays everything: all costs 0 -> tie -> the smallest codec (AV1).
    matrix = {"A": _matrix(("Apple", "hevc", 10, 0), ("Apple", "h264", 10, 0), ("Apple", "av1", 10, 0))}
    pid, reason = choose_codec_profile(
        1080, {"A": 1.0}, matrix, PROFS, per_user_platform_weights={"A": {"Apple": 1.0}})
    assert pid == 17 and reason["codec"] == "av1" and reason["cost"] == 0.0


def test_cold_matrix_defaults_to_efficient_codec():
    # No history at all -> every cost is the neutral none_p prior -> tie -> AV1 (most efficient).
    pid, _reason = choose_codec_profile(1080, {"A": 1.0}, {}, PROFS, per_user_platform_weights={})
    assert pid == 17


def test_no_candidate_at_tier_returns_none():
    # No 2160 profile among the candidates -> caller keeps its resolution-only pick.
    pid, reason = choose_codec_profile(2160, {"A": 1.0}, {}, PROFS, per_user_platform_weights={})
    assert pid is None and reason["reason"] == "no_candidate_at_tier"


# ── classifier ─────────────────────────────────────────────────────────────────────
def test_classify_profile_axes_from_name():
    assert classify_profile_axes(HEVC) == {"codec": "hevc", "res_tier": 1080,
                                           "name": "WEB-1080p (HEVC)", "id": 16}
    assert classify_profile_axes(H264)["codec"] == "h264"
    assert classify_profile_axes(AV1)["codec"] == "av1"


def test_classify_profile_axes_from_cf_scores_with_ban():
    # Name names no codec ('Combined'); CF scores steer HEVC (x265 +100) with AV1 banned (-10000).
    p = _prof(9, "Remux 2160p (Combined)", 2160, cf_scores={"x265 (HD)": 100, "AV1": -10000, "x264": 0})
    axes = classify_profile_axes(p)
    assert axes["codec"] == "hevc" and axes["res_tier"] == 2160


def test_classify_profile_axes_unknown_when_silent():
    p = _prof(1, "Remux 2160p", 2160)  # no codec in name, no CF scores
    assert classify_profile_axes(p)["codec"] == "unknown"


# ── fingerprint delegation + cost ────────────────────────────────────────────────────
def test_candidate_fingerprint_delegates_to_source_fingerprint():
    axes = {"codec": "hevc", "res_tier": 1080}
    assert candidate_fingerprint(axes) == source_fingerprint(video_codec="hevc", height=1080)
    assert candidate_fingerprint(axes) == ("hevc", "unknown", "none", "1080p_sdr", "unknown")


def test_viewer_transcode_cost_weights_and_none_prior():
    matrix = {"A": _matrix(("PS5", "hevc", 10, 0)), "B": {}}  # B has no data -> none_p
    weights = {"A": {"PS5": 1.0}, "B": {"Box": 1.0}}
    fp = ("hevc", "unknown", "none", "1080p_sdr", "unknown")
    # A: 0.5*0 (direct) ; B: 0.5*none_p(0.5) = 0.25 -> total 0.25
    cost = viewer_transcode_cost(fp, {"A": 0.5, "B": 0.5}, matrix, weights, none_p=0.5)
    assert cost == 0.25
