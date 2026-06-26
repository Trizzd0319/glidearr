"""Tests for acquisition.pilot_stepping — the pure decision slices of the Sonarr
stub-pilot search (ML Step 8). The service keeps the fetch + search/PUT/df-write loop;
these cover the extracted cores: profile ranking, interval-due, and the ladder step.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from scripts.managers.machine_learning.acquisition.pilot_stepping import (
    choose_pilot_profile,
    classify_release_outcome,
    next_pilot_profile,
    next_pilot_profile_descend,
    pilot_backoff_interval,
    pilot_search_due,
    profile_max_resolution,
    rank_pilot_profiles,
)


# ── classify_release_outcome (why a pilot acquires or not) ──────────────────────────
def _rel(res, *, rejected=False, rejections=None):
    return {"quality": {"quality": {"resolution": res}}, "rejected": rejected,
            "rejections": rejections or []}


def test_classify_no_results():
    d = classify_release_outcome([], floor_res=0)
    assert d["reason"] == "no_results" and d["total"] == 0 and d["rejection_reasons"] == []


def test_classify_below_floor():
    d = classify_release_outcome([_rel(480), _rel(576)], floor_res=720)
    assert d["reason"] == "below_floor"
    assert d["resolutions"] == [480, 576] and d["accepted"] == 0


def test_classify_no_resolution():
    # releases exist but none report a resolution (SD-only / odd) → distinct from below_floor.
    d = classify_release_outcome([_rel(0), {"quality": {}}], floor_res=0)
    assert d["reason"] == "no_resolution" and d["total"] == 2 and d["resolutions"] == []


def test_classify_available_when_one_release_not_rejected():
    d = classify_release_outcome(
        [_rel(1080), _rel(1080, rejected=True, rejections=["Sample"])], floor_res=0)
    assert d["reason"] == "available" and d["accepted"] == 1 and d["rejected"] == 1


def test_classify_rejected_hard_when_all_profile_independent():
    # size + blocklist + incomplete = a flip can't fix → rejected_hard (skippable).
    rels = [_rel(1080, rejected=True, rejections=["1.8 GB is larger than maximum allowed 910.0 MB"]),
            _rel(720, rejected=True, rejections=[{"reason": "Release is blocklisted"}]),
            _rel(480, rejected=True, rejections=["Not a Complete Release"])]
    d = classify_release_outcome(rels, floor_res=0)
    assert d["reason"] == "rejected_hard" and d["rescuable"] is False
    assert dict(d["rejection_reasons"])["Not a Complete Release"] == 1


def test_classify_rejected_rescuable_when_profile_dependent():
    # quality-not-wanted + CF score below minimum = clears after the profile flip → still searched.
    rels = [_rel(1080, rejected=True, rejections=["WEBDL-1080p is not wanted in profile"]),
            _rel(720, rejected=True, rejections=["Custom Formats X have score -28000 below minimum 0"])]
    d = classify_release_outcome(rels, floor_res=0)
    assert d["reason"] == "rejected" and d["rescuable"] is True


def test_classify_rejected_mixed_one_rescuable_searches():
    # one release hard (size), one rescuable (quality) → overall rescuable → "rejected".
    rels = [_rel(1080, rejected=True, rejections=["larger than maximum allowed"]),
            _rel(720, rejected=True, rejections=["WEBDL-720p is not wanted in profile"])]
    assert classify_release_outcome(rels, floor_res=0)["reason"] == "rejected"


def test_classify_batch_only_season_pack():
    # season pack: rejected ONLY for single-vs-pack scope (+ flip-clearable quality) → batch_only.
    rels = [_rel(720, rejected=True,
                 rejections=["Episode wasn't requested: 1x12", "WEBDL-720p is not wanted in profile"])]
    d = classify_release_outcome(rels, floor_res=0)
    assert d["reason"] == "rejected" and d["batch_only"] is True


def test_classify_not_batch_only_when_a_hard_reason_also_blocks():
    # wasn't-requested AND a hard size rejection → a SeasonSearch can't help → NOT batch_only.
    rels = [_rel(720, rejected=True,
                 rejections=["Episode wasn't requested: 1x12", "larger than maximum allowed"])]
    assert classify_release_outcome(rels, floor_res=0)["batch_only"] is False


def test_classify_not_batch_only_when_something_accepted():
    d = classify_release_outcome([_rel(1080)], floor_res=0)
    assert d["reason"] == "available" and d["batch_only"] is False


# ── profile_max_resolution ──────────────────────────────────────────────────────
def test_profile_max_resolution_incl_nested_and_allowed_guard():
    p = {"items": [
        {"allowed": True,  "quality": {"resolution": 720}},
        {"allowed": False, "quality": {"resolution": 1080}},                          # not allowed -> ignored
        {"allowed": True,  "items": [{"allowed": True, "quality": {"resolution": 2160}}]},  # nested grouped
        {"allowed": True,  "items": [{"allowed": False, "quality": {"resolution": 4320}}]}, # nested not allowed
    ]}
    assert profile_max_resolution(p) == 2160


def test_profile_max_resolution_empty_is_zero():
    assert profile_max_resolution({}) == 0
    assert profile_max_resolution({"items": [{"allowed": True, "quality": {}}]}) == 0


# ── rank_pilot_profiles ─────────────────────────────────────────────────────────
def _p(pid, res):
    return {"id": pid, "items": [{"allowed": True, "quality": {"resolution": res}}]}


def test_rank_pilot_profiles_ascending_floor_first():
    ranked = rank_pilot_profiles([_p(3, 2160), _p(1, 480), _p(2, 1080)])
    assert [p["id"] for p in ranked] == [1, 2, 3]   # rank 0 = floor (lowest res)


# ── pilot_search_due ────────────────────────────────────────────────────────────
_NOW = datetime(2026, 6, 10, 12, tzinfo=timezone.utc)
_IV = timedelta(hours=24)


def test_pilot_backoff_interval_off_is_byte_identical():
    base = timedelta(hours=24)
    # no config / disabled / missing enabled -> base interval unchanged, any attempts count
    for cfg in (None, {}, {"enabled": False, "base": 4}, {"base": 4}):
        for att in (0, 1, 5, 99):
            assert pilot_backoff_interval(base, att, backoff=cfg) == base


def test_pilot_backoff_interval_grows_and_caps():
    base = timedelta(hours=24)
    cfg = {"enabled": True, "base": 2.0, "cap_attempts": 3}
    assert pilot_backoff_interval(base, 0, backoff=cfg) == base            # first pass unchanged
    assert pilot_backoff_interval(base, 1, backoff=cfg) == base * 2        # 2^1
    assert pilot_backoff_interval(base, 2, backoff=cfg) == base * 4        # 2^2
    assert pilot_backoff_interval(base, 3, backoff=cfg) == base * 8        # 2^3 (cap)
    assert pilot_backoff_interval(base, 50, backoff=cfg) == base * 8       # capped at cap_attempts


def test_pilot_backoff_interval_exhausted_reprobe_is_finite():
    base = timedelta(hours=24)
    cfg = {"enabled": True, "base": 2.0, "cap_attempts": 6,
           "exhausted_after": 5, "reprobe_multiplier": 30.0}
    # below the exhausted threshold -> exponential
    assert pilot_backoff_interval(base, 4, backoff=cfg) == base * 16       # 2^4
    # at/over the threshold -> the long re-probe cooldown (FINITE, so re-probeable)
    assert pilot_backoff_interval(base, 5, backoff=cfg) == base * 30
    assert pilot_backoff_interval(base, 99, backoff=cfg) == base * 30
    # backoff only ever lengthens: base/mult clamped to >= 1.0
    cfg2 = {"enabled": True, "base": 0.1, "exhausted_after": 2, "reprobe_multiplier": 0.1}
    assert pilot_backoff_interval(base, 1, backoff=cfg2) == base           # base clamped to 1.0
    assert pilot_backoff_interval(base, 2, backoff=cfg2) == base           # reprobe clamped to 1.0


def test_pilot_search_due():
    assert pilot_search_due(None, _NOW, _IV) is True              # never searched
    assert pilot_search_due("", _NOW, _IV) is True                # blank
    assert pilot_search_due(pd.NaT, _NOW, _IV) is True            # NaT
    assert pilot_search_due("nonsense", _NOW, _IV) is True        # unparseable -> due
    assert pilot_search_due("2026-06-10T00:00:00Z", _NOW, _IV) is False  # 12h ago < 24h -> not due
    assert pilot_search_due("2026-06-08T00:00:00Z", _NOW, _IV) is True   # 60h ago >= 24h -> due


# ── next_pilot_profile (the ladder) ─────────────────────────────────────────────
_RANKED = [{"id": 11}, {"id": 12}, {"id": 13}]   # ranks 0,1,2 (floor .. ceiling)


def test_next_pilot_profile_floor_on_first_attempt():
    assert next_pilot_profile(attempts_done=0, current_pid=12, current_rank=1,
                              last_pid=None, ranked=_RANKED) == (11, "floor")


def test_next_pilot_profile_steps_up_when_prior_attempt_at_current():
    assert next_pilot_profile(attempts_done=1, current_pid=11, current_rank=0,
                              last_pid=11, ranked=_RANKED) == (12, "step_up")


def test_next_pilot_profile_at_ceiling_holds():
    assert next_pilot_profile(attempts_done=3, current_pid=13, current_rank=2,
                              last_pid=13, ranked=_RANKED) == (13, "at_ceiling")


def test_next_pilot_profile_holds_when_profile_changed_externally():
    # last attempt was at a DIFFERENT profile than current (someone changed it) -> hold
    assert next_pilot_profile(attempts_done=1, current_pid=12, current_rank=1,
                              last_pid=11, ranked=_RANKED) == (12, "hold")
    # no recorded last profile -> hold
    assert next_pilot_profile(attempts_done=1, current_pid=12, current_rank=1,
                              last_pid=None, ranked=_RANKED) == (12, "hold")


# ── max_rank cap (C4: low-likelihood stub ladder cap) ────────────────────────────
def test_next_pilot_profile_max_rank_none_is_byte_identical():
    # uncapped default reproduces the step-up exactly.
    assert next_pilot_profile(attempts_done=1, current_pid=11, current_rank=0,
                              last_pid=11, ranked=_RANKED, max_rank=None) == (12, "step_up")


def test_next_pilot_profile_max_rank_caps_the_climb():
    # cap at rank 1: from rank 0 we may step to rank 1...
    assert next_pilot_profile(attempts_done=1, current_pid=11, current_rank=0,
                              last_pid=11, ranked=_RANKED, max_rank=1) == (12, "step_up")
    # ...but from rank 1 the next tier (rank 2) exceeds the cap -> hold at ceiling.
    assert next_pilot_profile(attempts_done=2, current_pid=12, current_rank=1,
                              last_pid=12, ranked=_RANKED, max_rank=1) == (12, "at_ceiling")
    # cap at the floor (rank 0): a very-low-likelihood stub never climbs past the floor.
    assert next_pilot_profile(attempts_done=1, current_pid=11, current_rank=0,
                              last_pid=11, ranked=_RANKED, max_rank=0) == (11, "at_ceiling")
    # the floor attempt itself is never blocked by the cap.
    assert next_pilot_profile(attempts_done=0, current_pid=12, current_rank=1,
                              last_pid=None, ranked=_RANKED, max_rank=0) == (11, "floor")


# ── best-tier-first / space-divert pilot (deliverable C) ─────────────────────────
def _pq(pid, res):
    # profile with a NAMED quality so estimate_gb_for_profile resolves measured MiB/min
    return {"id": pid, "name": f"P{res}",
            "items": [{"allowed": True, "quality": {"resolution": res, "name": f"q{res}"}}]}


_BEST_FIRST_P = [_pq(13, 2160), _pq(12, 1080), _pq(11, 720)]   # highest resolution first
_MEASURED_P = {"q2160": 200.0, "q1080": 70.0, "q720": 30.0}    # est(100min)=19.53/6.84/2.93 GiB


def test_choose_pilot_profile_best_that_fits_with_no_likelihood_cap():
    # Plenty of room → the HIGHEST tier (id 13). There is NO cap argument: a pilot is never
    # gated by watch-likelihood, only by space — that is the whole point of the inversion.
    assert choose_pilot_profile(_BEST_FIRST_P, projected_free=30.0, reserve_gb=5.0,
                                runtime_min=100, measured=_MEASURED_P)["id"] == 13


def test_choose_pilot_profile_diverts_down_under_space_pressure():
    # free 10: 4K (−19.5) and 1080 (−6.84) breach the 5 GB reserve; 720 (−2.93) fits → id 11.
    assert choose_pilot_profile(_BEST_FIRST_P, projected_free=10.0, reserve_gb=5.0,
                                runtime_min=100, measured=_MEASURED_P)["id"] == 11


def test_choose_pilot_profile_none_when_even_floor_breaches_reserve():
    # free 5, reserve 5: even 720 (−2.93) breaches → None. The brain is HONEST (symmetric with
    # choose_jit_profile); the SERVICE decides forced-floor (grab anyway) vs skip-until-space.
    assert choose_pilot_profile(_BEST_FIRST_P, projected_free=5.0, reserve_gb=5.0,
                                runtime_min=100, measured=_MEASURED_P) is None


# ── next_pilot_profile_descend (best-tier-first across-run ladder) ───────────────
# ranked is floor-first: rank 0 = 720 (floor), rank 1 = 1080, rank 2 = 2160 (widest).
_RANKED_D = [{"id": 11}, {"id": 12}, {"id": 13}]


def test_descend_targets_the_space_ceiling_on_first_attempt():
    # No prior attempt (last_pid None) → search at the best-that-fits ceiling (rank 2 = 2160).
    assert next_pilot_profile_descend(start_rank=2, current_pid=5, current_rank=0,
                                      last_pid=None, ranked=_RANKED_D) == (13, "target")
    # A tighter space ceiling (rank 1) → target 1080, not 2160.
    assert next_pilot_profile_descend(start_rank=1, current_pid=5, current_rank=0,
                                      last_pid=None, ranked=_RANKED_D) == (12, "target")


def test_descend_steps_down_one_rung_when_current_tier_found_nothing():
    # Last run searched 2160 (rank 2) and grabbed nothing → divert down to 1080 (rank 1).
    assert next_pilot_profile_descend(start_rank=2, current_pid=13, current_rank=2,
                                      last_pid=13, ranked=_RANKED_D) == (12, "step_down")
    # Again from 1080 → 720 (rank 0, the floor).
    assert next_pilot_profile_descend(start_rank=2, current_pid=12, current_rank=1,
                                      last_pid=12, ranked=_RANKED_D) == (11, "step_down")


def test_descend_holds_at_floor_never_abandons_the_pilot():
    # At the floor (rank 0) with nothing grabbed → hold and keep re-searching (never give up).
    assert next_pilot_profile_descend(start_rank=2, current_pid=11, current_rank=0,
                                      last_pid=11, ranked=_RANKED_D) == (11, "at_floor")


def test_descend_never_climbs_above_the_space_ceiling():
    # Space shrank so the ceiling is now rank 0 (720). Even stepping down from a higher current
    # clamps to the ceiling — a pilot never searches above what space allows.
    assert next_pilot_profile_descend(start_rank=0, current_pid=13, current_rank=2,
                                      last_pid=13, ranked=_RANKED_D) == (11, "step_down")


def test_descend_retargets_ceiling_on_external_profile_change():
    # Profile changed outside our stepping (last_pid != current_pid) → re-target the ceiling.
    assert next_pilot_profile_descend(start_rank=2, current_pid=12, current_rank=1,
                                      last_pid=11, ranked=_RANKED_D) == (13, "target")


def test_descend_clamps_out_of_range_start_rank():
    # An out-of-range start_rank is clamped into [0, n-1] (defensive; callers pass an in-range rank).
    assert next_pilot_profile_descend(start_rank=99, current_pid=5, current_rank=0,
                                      last_pid=None, ranked=_RANKED_D) == (13, "target")   # → top
    assert next_pilot_profile_descend(start_rank=-5, current_pid=5, current_rank=0,
                                      last_pid=None, ranked=_RANKED_D) == (11, "target")   # → floor
