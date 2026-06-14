"""Tests for lifecycle.monitor_policy — the pure decision slices of the Radarr
monitored-missing triage (ML Step 8). The service keeps the scoring (credits +
score_movie) and the bulk movie/editor PUTs; these cover the extracted cores:
release-availability and the score routing.
"""
from __future__ import annotations

from datetime import datetime, timezone

from scripts.managers.machine_learning.lifecycle.monitor_policy import (
    _date_passed,
    release_available,
    triage_action,
)

_NOW = datetime(2026, 6, 10, tzinfo=timezone.utc)


# ── release_available ───────────────────────────────────────────────────────────
def test_release_available_trusts_is_available():
    assert release_available({"isAvailable": True}, _NOW) is True


def test_release_available_falls_back_to_past_release_dates():
    assert release_available({"physicalRelease": "2026-01-01T00:00:00Z"}, _NOW) is True
    assert release_available({"digitalRelease": "2026-01-01"}, _NOW) is True
    assert release_available({"status": "released"}, _NOW) is True


def test_release_not_available_when_future_or_missing():
    assert release_available({"physicalRelease": "2027-01-01T00:00:00Z", "status": "announced"}, _NOW) is False
    assert release_available({"status": "announced"}, _NOW) is False
    assert release_available({"digitalRelease": "not-a-date", "status": "inCinemas"}, _NOW) is False


# ── _date_passed hardening ───────────────────────────────────────────────────────
def test_date_passed_blank_and_unparseable():
    assert _date_passed("", _NOW) is False
    assert _date_passed(None, _NOW) is False
    assert _date_passed("not-a-date", _NOW) is False


def test_date_passed_truthy_non_str_returns_false_not_attributeerror():
    # A truthy non-str (e.g. an int epoch) would AttributeError on .replace and
    # escape the (ValueError, TypeError) guard. Hardened to return False instead.
    assert _date_passed(1234567890, _NOW) is False
    assert _date_passed(1234567890.0, _NOW) is False
    assert _date_passed(["2026-01-01"], _NOW) is False


def test_release_available_survives_non_str_release_date():
    # The non-str must not crash release_available's _date_passed fallbacks.
    assert release_available({"physicalRelease": 1234567890, "status": "announced"}, _NOW) is False
    assert release_available({"digitalRelease": 1234567890, "status": "released"}, _NOW) is True


# ── triage_action ───────────────────────────────────────────────────────────────
def _act(score, *, has_keep_tag=False, credits_fetched=True, cur_profile_id=1, hd720p_id=99,
         household_watched=False):
    return triage_action(score=score, has_keep_tag=has_keep_tag, credits_fetched=credits_fetched,
                         cur_profile_id=cur_profile_id, hd720p_id=hd720p_id,
                         watch_threshold=60, unmonitor_below=20, household_watched=household_watched)


def test_triage_below_floor_routing_and_precedence():
    # keep_skip outranks defer outranks unmonitor, all below the floor
    assert _act(10, has_keep_tag=True, credits_fetched=False) == "keep_skip"
    assert _act(10, has_keep_tag=False, credits_fetched=False) == "defer"
    assert _act(10, has_keep_tag=False, credits_fetched=True) == "unmonitor"


def test_triage_marginal_adjusts_then_searches():
    # >= floor, < watch, has a 720p target on a different profile -> adjust
    assert _act(40, cur_profile_id=1, hd720p_id=99) == "adjust_and_search"


def test_triage_marginal_searches_when_no_adjust_target():
    # no hd720p target, or already on it -> plain search (no profile change)
    assert _act(40, hd720p_id=None) == "search"
    assert _act(40, cur_profile_id=99, hd720p_id=99) == "search"


def test_triage_good_score_searches():
    assert _act(75) == "search"
    assert _act(60) == "search"   # exactly the watch threshold is not < watch -> search


def test_triage_household_watched_never_unmonitors():
    # A household-watched movie that lost its file is always RE-ACQUIRED, never
    # unmonitored/deferred/keep-skipped — even at a low score / no credits / keep-tag.
    # (no 720p target -> plain search)
    assert _act(5, household_watched=True, hd720p_id=None) == "search"
    assert _act(5, has_keep_tag=True, household_watched=True, hd720p_id=None) == "search"      # overrides keep_skip
    assert _act(5, credits_fetched=False, household_watched=True, hd720p_id=None) == "search"  # overrides defer
    # low score WITH a 720p target on a different profile -> adjust-down then search (still re-acquire)
    assert _act(5, cur_profile_id=1, hd720p_id=99, household_watched=True) == "adjust_and_search"
    # in every low-score combo, a watched movie re-acquires (never unmonitor/defer/keep_skip)
    for s in (0, 5, 19):
        assert _act(s, has_keep_tag=True, credits_fetched=False, household_watched=True) in ("search", "adjust_and_search")
    # baseline (no override): the same low-score movie unmonitors
    assert _act(5, household_watched=False) == "unmonitor"
