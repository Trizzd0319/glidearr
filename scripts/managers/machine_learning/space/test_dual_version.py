"""Tests for the dual-version policy: 1080p is the PRIORITY baseline (score/space-adaptive, capped
below 4K — 1080p OR 720p whichever the shared ladder justifies), and 4K is the BONUS layer (kept
only when space + keep/watchability warrant it, dropped FIRST under pressure)."""
from __future__ import annotations

import pytest

from scripts.managers.machine_learning.space.dual_version import (
    hd_capped_likelihood, hd_target_resolution, pick_hd_profile, wants_uhd, should_drop_uhd,
    plan_hd_baseline,
)


def _profile(name, res):
    return {"id": res, "name": name, "items": [{"allowed": True, "quality": {"name": name, "resolution": res}}]}


_PROFILES = [_profile("SD", 480), _profile("HD-720", 720), _profile("HD-1080", 1080), _profile("UHD", 2160)]
_BOTH = {"movies": {"4k_policy": "both"}}


# Ladder-id profiles (the cap matches Radarr ladder profile ids, not resolutions): default ladder
# rung 8 = 1080p (likelihood 55), rung 9 = 2160p/4K (likelihood 70).
def _lprofile(pid, res):
    return {"id": pid, "name": f"p{pid}", "items": [{"allowed": True, "quality": {"resolution": res}}]}


_LADDER_PROFS = [_lprofile(8, 1080), _lprofile(9, 2160)]


@pytest.fixture
def _proactive_cfg(monkeypatch):
    for v in ("RECOMMENDARR_RELOCATION_CONSENT", "GLIDEARR_RELOCATION_CONSENT"):
        monkeypatch.delenv(v, raising=False)
    return {"routing": {"reorg_mode": "same_instance", "movies": {"proactive_4k": True, "4k_policy": "both"}},
            "relocation_consent": True}


# ── hd_capped_likelihood (the single-authority upgrade cap) ───────────────────
def test_hd_cap_noop_when_proactive_off():
    assert hd_capped_likelihood(90, _LADDER_PROFS, {}) == 90        # default off → byte-for-byte unchanged


def test_hd_cap_lowers_below_first_present_4k_rung(_proactive_cfg):
    assert hd_capped_likelihood(90, _LADDER_PROFS, _proactive_cfg) == 89.0   # symmetric ladder: first present 4K rung (p9) now at 90 → 89


def test_hd_cap_leaves_already_low_likelihood(_proactive_cfg):
    assert hd_capped_likelihood(50, _LADDER_PROFS, _proactive_cfg) == 50


def test_hd_cap_noop_when_no_4k_profile_present(_proactive_cfg):
    assert hd_capped_likelihood(90, [_lprofile(8, 1080)], _proactive_cfg) == 90   # no 4K rung → unchanged


# ── adaptive baseline tier (shared ladder, clamped below 4K) ──────────────────
def test_hd_target_resolution_clamped_below_4k():
    assert hd_target_resolution(95) == 1080      # score warrants 2160 -> clamped to 1080
    assert hd_target_resolution(40) == 1080
    assert hd_target_resolution(25) == 720       # mid score -> 720, NOT forced to 1080
    assert hd_target_resolution(5) == 480
    assert hd_target_resolution(None) == 1080     # no score yet -> watchable baseline at the cap


def test_pick_hd_profile_follows_score_tier():
    assert pick_hd_profile(_PROFILES, score=80)["name"] == "HD-1080"
    assert pick_hd_profile(_PROFILES, score=25)["name"] == "HD-720"     # 720p justified, not 1080
    assert pick_hd_profile([_profile("UHD", 2160)], score=80) is None    # no <=1080 profile


def test_pick_hd_profile_res_cap_pushes_lower_under_pressure():
    assert pick_hd_profile(_PROFILES, score=80, res_cap=720)["name"] == "HD-720"


# ── 4K bonus decision (wants_uhd) ─────────────────────────────────────────────
def test_wants_uhd_keep_tagged_with_space():
    assert wants_uhd(keep_tagged=True, score=0, space_allows=True) is True


def test_wants_uhd_high_score_with_space():
    assert wants_uhd(keep_tagged=False, score=80, space_allows=True) is True


def test_wants_uhd_false_without_space():
    assert wants_uhd(keep_tagged=True, score=99, space_allows=False) is False    # space gates both


def test_wants_uhd_false_low_score():
    assert wants_uhd(keep_tagged=False, score=40, space_allows=True) is False    # < 70 threshold


def test_wants_uhd_false_when_no_remote_viewer():
    assert wants_uhd(keep_tagged=False, score=90, space_allows=True, can_remote_play=False) is False


# ── eviction: drop the 4K FIRST (the 1080p baseline survives) ─────────────────
def test_should_drop_uhd_below_floor_even_when_keep_tagged():
    # under pressure the 4K bonus goes first; the title is preserved at 1080p
    assert should_drop_uhd(keep_tagged=True, score=99, free_below_floor=True) is True


def test_should_drop_uhd_keeps_tagged_when_not_pressured():
    assert should_drop_uhd(keep_tagged=True, score=10, free_below_floor=False) is False


def test_should_drop_uhd_when_watchability_drops():
    assert should_drop_uhd(keep_tagged=False, score=40, free_below_floor=False) is True   # < 70


def test_should_drop_uhd_keeps_high_score():
    assert should_drop_uhd(keep_tagged=False, score=85, free_below_floor=False) is False


# ── the 1080p baseline planner ────────────────────────────────────────────────
def test_plan_hd_baseline_queues_when_both_and_missing():
    plan, _ = plan_hd_baseline(tmdb=1, title="Dune", routing=_BOTH, hd_profiles=_PROFILES,
                               hd_instance="standard", hd_root="/m/std", score=80)
    assert plan is not None and plan["profile"]["name"] == "HD-1080"
    assert plan["instance"] == "standard" and plan["root_folder"] == "/m/std"


def test_plan_hd_baseline_adaptive_to_lower_score():
    plan, _ = plan_hd_baseline(tmdb=1, title="B", routing=_BOTH, hd_profiles=_PROFILES,
                               hd_instance="standard", hd_root="/m/std", score=25)
    assert plan["profile"]["name"] == "HD-720"          # mid score -> 720p baseline


def test_plan_hd_baseline_skips_when_present():
    plan, reason = plan_hd_baseline(tmdb=1, title="X", routing=_BOTH, hd_profiles=_PROFILES,
                                    hd_instance="standard", hd_root="/m/std", already_present=True)
    assert plan is None and "already present" in reason


def test_plan_hd_baseline_skips_when_not_both():
    plan, reason = plan_hd_baseline(tmdb=1, title="X", routing={"movies": {"4k_policy": "highest_only"}},
                                    hd_profiles=_PROFILES, hd_instance="standard", hd_root="/m/std")
    assert plan is None and "both" in reason


def test_plan_hd_baseline_refuses_without_capped_profile():
    plan, _ = plan_hd_baseline(tmdb=1, title="X", routing=_BOTH, hd_profiles=[_profile("UHD", 2160)],
                               hd_instance="standard", hd_root="/m/std", score=80)
    assert plan is None
