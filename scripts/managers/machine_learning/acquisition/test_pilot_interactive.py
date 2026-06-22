"""Pure decision slices of the interactive-search pilot model: read all availability from one manual
search, jump to the lowest available resolution, and gate UNACQUIRABLE re-checks on new-indexer or a
time cooldown."""
from __future__ import annotations

import pandas as pd

from scripts.managers.machine_learning.acquisition.pilot_stepping import (
    available_release_resolutions,
    choose_lowest_available_tier,
    indexer_fingerprint,
    pilot_recheck_due,
)

# floor→widest ladder, [(profile_id, max_resolution)] ascending
LADDER = [(11, 480), (12, 720), (13, 1080), (14, 2160)]


def _rel(res, *, rejected=False, seeders=10, size=1_000, guid=None):
    return {"guid": guid or f"g{res}-{seeders}", "indexerId": 1, "rejected": rejected,
            "seeders": seeders, "size": size,
            "quality": {"quality": {"resolution": res}}}


# ── availability read ─────────────────────────────────────────────────────────────────
def test_resolutions_distinct_and_sorted():
    rels = [_rel(1080), _rel(480), _rel(1080), _rel(720)]
    assert available_release_resolutions(rels) == [480, 720, 1080]


def test_resolutions_empty_means_unacquirable():
    assert available_release_resolutions([]) == []
    assert available_release_resolutions([{"quality": {}}, {}]) == []   # no resolution → ignored


# ── lowest-available tier jump (no release-level selection — that's Sonarr's CF) ─────────
def test_jumps_to_lowest_available_tier():
    # no SD, no 720 — only 1080 + 2160 exist → search 1080, mapped to the 1080 profile (13).
    res, pid = choose_lowest_available_tier([_rel(1080), _rel(2160)], LADDER)
    assert res == 1080 and pid == 13


def test_picks_floor_when_available():
    res, pid = choose_lowest_available_tier([_rel(480), _rel(1080)], LADDER)
    assert res == 480 and pid == 11


def test_tier_choice_ignores_seeders_and_rejected():
    # a rejected, high-seeder 480 still makes 480 the lowest available tier — release selection is
    # deferred to Sonarr's CF, so we don't rank releases here.
    res, pid = choose_lowest_available_tier([_rel(480, rejected=True, seeders=99), _rel(720)], LADDER)
    assert res == 480 and pid == 11


def test_floor_res_skips_below_minimum():
    # floor_res=720 → ignore the 480, search the 1080 (next available at/above floor)
    res, pid = choose_lowest_available_tier([_rel(480), _rel(1080)], LADDER, floor_res=720)
    assert res == 1080 and pid == 13


def test_no_releases_returns_none():
    assert choose_lowest_available_tier([], LADDER) is None
    assert choose_lowest_available_tier([{"quality": {}}], LADDER) is None


def test_resolution_above_ladder_maps_to_widest():
    res, pid = choose_lowest_available_tier([_rel(4320)], LADDER)   # 8K, nothing covers it
    assert res == 4320 and pid == 14                                # widest tier


# ── indexer fingerprint ─────────────────────────────────────────────────────────────────
def test_fingerprint_sorted_enabled_only():
    ix = [{"id": 3}, {"id": 1}, {"id": 2, "enableInteractiveSearch": False}, {"name": "no-id"}]
    assert indexer_fingerprint(ix) == [1, 3]                                # 2 excluded, no-id dropped


# ── UNACQUIRABLE re-check gate (dead until new indexer OR a week) ────────────────────────
NOW = pd.Timestamp("2026-06-21T00:00:00Z")
WEEK = pd.Timedelta(days=7)


def test_recheck_blocked_within_cooldown_same_indexers():
    flagged = (NOW - pd.Timedelta(days=2)).isoformat()
    assert pilot_recheck_due(flagged, [1, 2], NOW, [1, 2], cooldown=WEEK) is False


def test_recheck_due_after_cooldown():
    flagged = (NOW - pd.Timedelta(days=8)).isoformat()
    assert pilot_recheck_due(flagged, [1, 2], NOW, [1, 2], cooldown=WEEK) is True


def test_recheck_due_when_new_indexer_added():
    flagged = (NOW - pd.Timedelta(days=1)).isoformat()                      # still within cooldown
    assert pilot_recheck_due(flagged, [1, 2], NOW, [1, 2, 5], cooldown=WEEK) is True  # indexer 5 is new


def test_recheck_due_when_flag_timestamp_missing():
    assert pilot_recheck_due(None, [1], NOW, [1], cooldown=WEEK) is True


def test_recheck_removed_indexer_does_not_trigger():
    # an indexer going AWAY is not a reason to re-check (fewer sources, not more)
    flagged = (NOW - pd.Timedelta(days=1)).isoformat()
    assert pilot_recheck_due(flagged, [1, 2, 3], NOW, [1, 2], cooldown=WEEK) is False
