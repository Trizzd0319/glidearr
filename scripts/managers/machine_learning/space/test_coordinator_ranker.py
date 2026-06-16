"""Tests for space.coordinator_ranker — the unified delete-pool ranking (ML Step 7a)
and the optional recency weighting (C3). The default ranking (no recency ramp) must be
byte-identical to the bare ``(score, critic, -size)`` order the coordinator has always
used; the ramp only sinks recently-watched files to the bottom of the sweep.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.managers.machine_learning.space.coordinator_ranker import (
    critic_sort,
    recency_bonus,
    select_for_target,
)

_NOW = datetime(2026, 6, 10, 12, tzinfo=timezone.utc)


def _iso(days_ago):
    return (_NOW - timedelta(days=days_ago)).isoformat()


def _c(score, size_gb, *, critic=None, watched=None, fid=None):
    return {"score": score, "size_gb": size_gb, "critic": critic,
            "last_watched_at": watched, "fid": fid}


# ── critic_sort ─────────────────────────────────────────────────────────────────
def test_critic_sort():
    assert critic_sort(None) == 5.0
    assert critic_sort("7.5") == 7.5
    assert critic_sort("nope") == 5.0


# ── default ranking (no recency) is unchanged ────────────────────────────────────
def test_select_for_target_default_order():
    pool = [_c(8, 2, fid="hi"), _c(2, 3, fid="lo"), _c(5, 1, fid="mid")]
    sel, proj = select_for_target(pool, need_gb=4.0)
    # lowest score first: lo(2) then mid(5); 3+1=4 reaches the target.
    assert [c["fid"] for c in sel] == ["lo", "mid"] and proj == 4.0


# ── uhd_first bias (evict baseline-backed 4K copies before any whole title) ──────
def _uc(score, size_gb, *, fid=None, uhd=False):
    return {"score": score, "size_gb": size_gb, "critic": None,
            "last_watched_at": None, "fid": fid, "is_uhd_copy": uhd}


def test_uhd_first_off_is_byte_identical():
    # is_uhd_copy flags present but uhd_first unset → exact same order as the bare key.
    pool = [_uc(8, 2, fid="hi", uhd=True), _uc(2, 3, fid="lo"), _uc(5, 1, fid="mid", uhd=True)]
    base, _ = select_for_target([dict(c) for c in pool], need_gb=4.0)
    biased_off, _ = select_for_target([dict(c) for c in pool], need_gb=4.0, uhd_first=False)
    assert [c["fid"] for c in base] == [c["fid"] for c in biased_off]


def test_uhd_first_evicts_4k_ahead_of_lower_score_title():
    # a high-watchability 4K copy (score 95) still goes BEFORE a cold whole title (score 2),
    # because deleting the 4K copy loses no title (its baseline survives) — pure reclaim.
    pool = [_uc(95, 5, fid="uhd", uhd=True), _uc(2, 5, fid="cold_title")]
    sel, _ = select_for_target(pool, need_gb=4.0, uhd_first=True)
    assert sel[0]["fid"] == "uhd" and sel[0]["is_uhd_copy"] is True


def test_uhd_first_within_group_keeps_lowest_watchability_first():
    pool = [_uc(90, 5, fid="hi_uhd", uhd=True), _uc(10, 5, fid="lo_uhd", uhd=True)]
    sel, _ = select_for_target(pool, need_gb=4.0, uhd_first=True)
    assert sel[0]["fid"] == "lo_uhd"                       # lowest-watchability 4K copy goes first


def test_uhd_first_ignores_unflagged_4k_only_title():
    # a 2160p copy with NO surviving baseline is never flagged is_uhd_copy → ranks on score only.
    pool = [_uc(2, 5, fid="cold_title"), _uc(8, 5, fid="uhd_only", uhd=False)]
    sel, _ = select_for_target(pool, need_gb=4.0, uhd_first=True)
    assert sel[0]["fid"] == "cold_title"                  # no evict-first priority without the flag


def test_select_for_target_recency_off_is_byte_identical():
    pool = [_c(2, 3, watched=_iso(0), fid="lo"), _c(8, 2, watched=_iso(0), fid="hi")]
    # ramp absent / disabled / now omitted -> identical bare-score order (lo first).
    for kw in ({}, {"recency_ramp": {"enabled": False}, "now": _NOW},
               {"recency_ramp": {"enabled": True, "weight": 100}, "now": None}):
        sel, _ = select_for_target([dict(c) for c in pool], need_gb=1.0, **kw)
        assert sel[0]["fid"] == "lo"


# ── recency_bonus ─────────────────────────────────────────────────────────────────
def test_recency_bonus_decays_and_guards():
    import math
    ramp = {"weight": 10.0, "half_life_days": 30}
    fresh = recency_bonus({"last_watched_at": _iso(0)}, ramp, _NOW)
    aged = recency_bonus({"last_watched_at": _iso(30)}, ramp, _NOW)
    assert abs(fresh - 10.0) < 1e-9                   # watched now -> full weight
    assert abs(aged - 10.0 * math.exp(-1)) < 1e-9    # one time-constant -> 1/e (A1's decay shape)
    assert recency_bonus({}, ramp, _NOW) == 0.0                      # no anchor -> 0
    assert recency_bonus({"last_watched_at": "bad"}, ramp, _NOW) == 0.0  # unparseable -> 0


# ── recency on: a freshly-watched low-score file is protected ─────────────────────
def test_select_for_target_recency_protects_fresh():
    ramp = {"enabled": True, "weight": 100.0, "half_life_days": 30}
    # 'lo' has the lowest base score but was watched today; 'cold' is mid-score, watched
    # a year ago. With recency the fresh file's +100 bonus lifts it above cold.
    pool = [_c(2, 3, watched=_iso(0), fid="lo"), _c(5, 3, watched=_iso(365), fid="cold")]
    sel, _ = select_for_target(pool, need_gb=1.0, recency_ramp=ramp, now=_NOW)
    assert sel[0]["fid"] == "cold"   # cold deleted first; the just-watched file is spared


# ── tier_size bucketing (C4: biggest reclaim within a watchability tier) ──────────
def test_select_for_target_tier_size_off_is_byte_identical():
    pool = [_c(8, 9, fid="hi"), _c(2, 1, fid="lo"), _c(5, 5, fid="mid")]
    base, _ = select_for_target([dict(c) for c in pool], need_gb=2.0)
    for ts in (None, 0, 0.0):
        sel, _ = select_for_target([dict(c) for c in pool], need_gb=2.0, tier_size=ts)
        assert [c["fid"] for c in sel] == [c["fid"] for c in base]


def test_select_for_target_tier_size_buckets_biggest_first():
    # scores 1,2,3 all fall in one tier of width 10 -> floor(x/10)==0 for all; within
    # the tier the BIGGEST file goes first (fewer deletes to hit the target).
    pool = [_c(1, 2, fid="small"), _c(2, 8, fid="big"), _c(3, 4, fid="mid")]
    sel, proj = select_for_target(pool, need_gb=6.0, tier_size=10.0)
    assert sel[0]["fid"] == "big" and proj == 8.0 and len(sel) == 1  # one big delete vs several
    # a higher tier is still swept only after the lower tier — bucketing respects score.
    pool2 = [_c(1, 1, fid="t0"), _c(50, 99, fid="t5")]   # different tiers (0 vs 5)
    sel2, _ = select_for_target(pool2, need_gb=1.0, tier_size=10.0)
    assert sel2[0]["fid"] == "t0"
