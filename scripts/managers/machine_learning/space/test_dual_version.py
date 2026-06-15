"""Tests for the dual-version HD-copy planner — decides whether/how to acquire the second
(HD, <=1080p) copy of a UHD movie for remote play, with the cap that stops it making a
second 4K copy."""
from __future__ import annotations

from scripts.managers.machine_learning.space.dual_version import plan_hd_copy, pick_hd_profile


def _profile(name, res):
    return {"id": res, "name": name, "items": [{"allowed": True, "quality": {"name": name, "resolution": res}}]}


_PROFILES = [_profile("SD", 480), _profile("HD-720", 720), _profile("HD-1080", 1080), _profile("UHD", 2160)]
_BOTH = {"movies": {"4k_policy": "both", "4k_dual_min_score": 0}}


# ── profile selection (the cap) ───────────────────────────────────────────────
def test_pick_hd_profile_highest_under_1080():
    assert pick_hd_profile(_PROFILES)["name"] == "HD-1080"


def test_pick_hd_profile_none_when_only_4k():
    assert pick_hd_profile([_profile("UHD", 2160)]) is None


# ── plan_hd_copy ──────────────────────────────────────────────────────────────
def test_queues_hd_copy_when_uhd_and_both():
    plan, _ = plan_hd_copy(tmdb=1, title="Dune", is_uhd=True, score=80, routing=_BOTH,
                           hd_profiles=_PROFILES, hd_instance="standard", hd_root="/m/std")
    assert plan is not None
    assert plan["instance"] == "standard" and plan["root_folder"] == "/m/std"
    assert plan["profile"]["name"] == "HD-1080"        # capped at 1080p


def test_skips_when_policy_not_both():
    plan, reason = plan_hd_copy(tmdb=1, title="X", is_uhd=True, score=80,
                                routing={"movies": {"4k_policy": "highest_only"}},
                                hd_profiles=_PROFILES, hd_instance="standard", hd_root="/m/std")
    assert plan is None and "both" in reason


def test_skips_when_not_uhd():
    plan, reason = plan_hd_copy(tmdb=1, title="X", is_uhd=False, score=80, routing=_BOTH,
                                hd_profiles=_PROFILES, hd_instance="standard", hd_root="/m/std")
    assert plan is None and "UHD" in reason


def test_score_gate_blocks_low_score():
    routing = {"movies": {"4k_policy": "both", "4k_dual_min_score": 50}}
    plan, reason = plan_hd_copy(tmdb=1, title="X", is_uhd=True, score=30, routing=routing,
                                hd_profiles=_PROFILES, hd_instance="standard", hd_root="/m/std")
    assert plan is None and "score" in reason


def test_score_gate_allows_high_score():
    routing = {"movies": {"4k_policy": "both", "4k_dual_min_score": 50}}
    plan, _ = plan_hd_copy(tmdb=1, title="X", is_uhd=True, score=70, routing=routing,
                           hd_profiles=_PROFILES, hd_instance="standard", hd_root="/m/std")
    assert plan is not None


def test_skips_when_already_on_hd():
    plan, _ = plan_hd_copy(tmdb=1, title="X", is_uhd=True, score=80, routing=_BOTH,
                           hd_profiles=_PROFILES, hd_instance="standard", hd_root="/m/std", already_on_hd=True)
    assert plan is None


def test_refuses_without_an_hd_capped_profile():
    # only a 4K profile available → refuse rather than make a second 4K copy
    plan, reason = plan_hd_copy(tmdb=1, title="X", is_uhd=True, score=80, routing=_BOTH,
                                hd_profiles=[_profile("UHD", 2160)], hd_instance="standard", hd_root="/m/std")
    assert plan is None and "1080" in reason


def test_skips_without_hd_instance_or_root():
    plan, reason = plan_hd_copy(tmdb=1, title="X", is_uhd=True, score=80, routing=_BOTH,
                                hd_profiles=_PROFILES, hd_instance="", hd_root="")
    assert plan is None and "HD instance" in reason
