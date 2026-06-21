"""Tests for lifecycle.saga_retention.compute_saga_gates — engagement-derived per-saga deletion
holds. The engagement bar (≥threshold / started-within-grace) is applied UPSTREAM; here ``watched``
= passed, ``started`` = engaged-not-passed, ``watchlist`` = intent. Scenario mirrors the household:
Aiden ahead, Raina/Randa behind/climbing on one MCU-shaped saga."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.managers.machine_learning.lifecycle.saga_retention import compute_saga_gates

NOW = datetime(2026, 6, 20, tzinfo=timezone.utc)


def _ago(days):
    return NOW - timedelta(days=days)


# movies 1,2,3 (ranks 0-2) then shows 100,101 (ranks 3-4) — one MCU-shaped saga.
MCU = {"mcu": {"movies": {1: 0, 2: 1, 3: 2}, "shows": {100: 3, 101: 4}}}


def _gates(per_user, member_sets=MCU, **kw):
    kw.setdefault("dormancy_days", 90)
    kw.setdefault("expiry_boost_days", 30)
    return compute_saga_gates(member_sets, per_user, now=NOW, **kw)


# ── engagement + per-title hold ───────────────────────────────────────────────────
def test_watched_holds_only_unreached_members():
    # Aiden watched movie 1 → engaged; he's the only gate user, so 1 (passed) is freed and the
    # rest of the saga (2,3 + both shows) is held until he reaches them.
    out = _gates({"aiden": {"watched": {"movies": {1: _ago(1)}}}})
    assert out["movies"] == {2: ["mcu"], 3: ["mcu"]}
    assert out["shows"] == {100: ["mcu"], 101: ["mcu"]}
    assert out["gate_user_count"] == {"mcu": 1}


def test_not_engaged_user_does_not_gate():
    # watched only a NON-member (999) → not engaged with the saga → no hold at all.
    out = _gates({"bob": {"watched": {"movies": {999: _ago(1)}}}})
    assert out["movies"] == {} and out["shows"] == {} and out["gate_user_count"] == {}


def test_fully_caught_up_holds_nothing():
    # Aiden has passed every member → engaged (counts as a gate user) but nothing is held.
    out = _gates({"aiden": {"watched": {"movies": {1: _ago(1), 2: _ago(1), 3: _ago(1)},
                                        "shows": {100: _ago(1), 101: _ago(1)}}}})
    assert out["movies"] == {} and out["shows"] == {} and out["gate_user_count"] == {"mcu": 1}


def test_started_is_engaged_but_not_passed():
    # a STARTED (sub-threshold, recent) movie 1 engages Aiden but is NOT passed → it stays held too.
    out = _gates({"aiden": {"started": {"movies": {1: _ago(1)}}}})
    assert 1 in out["movies"] and out["movies"][1] == ["mcu"]


# ── dormancy ──────────────────────────────────────────────────────────────────────
def test_dormant_viewer_dropped():
    # last saga watch 200 days ago > 90-day dormancy → Aiden drops from the gate → nothing held.
    out = _gates({"aiden": {"watched": {"movies": {1: _ago(200)}}}})
    assert out["movies"] == {} and out["gate_user_count"] == {}


def test_two_viewers_behind_at_different_points():
    # Aiden passed 1+2 (climbing), Raina passed only 1 — both still climbing, both recent.
    out = _gates({
        "aiden": {"watched": {"movies": {1: _ago(2), 2: _ago(1)}}},
        "raina": {"watched": {"movies": {1: _ago(3)}}},
    })
    # 2 held (Raina hasn't reached it); 3 + shows held (neither has). 1 freed (both passed).
    assert out["movies"] == {2: ["mcu"], 3: ["mcu"]}
    assert out["shows"] == {100: ["mcu"], 101: ["mcu"]}
    assert out["gate_user_count"] == {"mcu": 2}


# ── watchlist: prefix scope + windowed expiry ───────────────────────────────────────
def test_watchlist_only_holds_prefix_up_to_watchlisted_title():
    # Randa watchlisted movie 2 (rank 1) → holds the prefix (ranks 0..1 = movies 1,2) ONLY;
    # later members (movie 3, shows) are out of her scope.
    out = _gates({"randa": {"watchlist": {"movies": {2: _ago(1)}}}})
    assert out["movies"] == {1: ["mcu"], 2: ["mcu"]}
    assert out["shows"] == {}


def test_watchlist_windowed_expires_but_indefinite_holds():
    stale = {"randa": {"watchlist": {"movies": {2: _ago(200)}}}}            # added 200d ago
    assert _gates(stale, watchlist_hold_policy="windowed")["movies"] == {}   # expired → released
    held = _gates(stale, watchlist_hold_policy="indefinite")["movies"]
    assert held == {1: ["mcu"], 2: ["mcu"]}                                  # intent never expires


# ── "use it or lose it" expiring set ────────────────────────────────────────────────
def test_expiring_set_within_boost_window():
    # last watch 70d ago → release at 90d (20d out), boost window 30d → inside it → expiring now.
    out = _gates({"aiden": {"watched": {"movies": {1: _ago(70)}}}})
    assert out["expiring_by_user"] == {"aiden": {"movies": [2, 3], "shows": [100, 101]}}


def test_not_expiring_when_recent():
    out = _gates({"aiden": {"watched": {"movies": {1: _ago(1)}}}})           # fresh → not near release
    assert out["expiring_by_user"] == {}


# ── crossover, quorum, excludes, fail-open ──────────────────────────────────────────
def test_crossover_title_held_by_union_of_sagas():
    members = {"a": {"movies": {1: 0, 2: 1}, "shows": {}},
               "b": {"movies": {2: 0, 3: 1}, "shows": {}}}                    # movie 2 in both
    out = _gates({"u": {"watched": {"movies": {1: _ago(1), 3: _ago(1)}}}}, member_sets=members)
    assert out["movies"] == {2: ["a", "b"]}                                   # held by both sagas


def test_quorum_releases_once_fraction_passed():
    members = {"s": {"movies": {1: 0, 2: 1}, "shows": {}}}
    per = {"alice": {"watched": {"movies": {1: _ago(1), 2: _ago(1)}}},
           "bob": {"watched": {"movies": {1: _ago(1)}}}}                      # bob hasn't passed 2
    assert _gates(per, member_sets=members)["movies"] == {2: ["s"]}           # default: held for bob
    relaxed = _gates(per, member_sets=members, quorum={"enabled": True, "fraction": 0.5})
    assert relaxed["movies"] == {}                                           # 1/2 passed ≥ 0.5 → released


def test_exclude_users_subtracts_from_gate():
    out = _gates({"aiden": {"watched": {"movies": {1: _ago(1)}}}}, exclude_users=["aiden"])
    assert out["movies"] == {} and out["gate_user_count"] == {}


def test_empty_inputs_fail_open():
    assert compute_saga_gates({}, {}, now=NOW) == {
        "movies": {}, "shows": {}, "gate_user_count": {}, "expiring_by_user": {}}
    assert _gates({})["movies"] == {}                                        # members but no users
