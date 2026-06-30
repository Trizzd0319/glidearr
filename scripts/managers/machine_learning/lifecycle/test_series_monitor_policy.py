"""Tests for lifecycle.series_monitor_policy.series_monitor_action — the pure routing of the Sonarr
monitor-by-watchability policy (monitor / unmonitor / hold / defer)."""
from __future__ import annotations

from scripts.managers.machine_learning.lifecycle.series_monitor_policy import series_monitor_action


def _act(monitored, score, *, has_score=True, keep_tagged=False, watched=False,
         promote=35, floor=20, age_days=0, dwell=0):
    return series_monitor_action(
        monitored=monitored, score=score, has_score=has_score, keep_tagged=keep_tagged,
        watched=watched, promote_threshold=promote, demote_floor=floor,
        age_days=age_days, dwell_days=dwell)


def test_unscored_defers():
    assert _act(True, None, has_score=False) == "defer"
    assert _act(False, None, has_score=False) == "defer"


def test_monitored_below_floor_unmonitors_when_dwell_met():
    assert _act(True, 12, floor=20, dwell=0) == "unmonitor"          # the empty-tail case
    assert _act(True, 19.9, floor=20, dwell=0) == "unmonitor"


def test_monitored_in_band_holds():
    # [floor=20, promote=35) is sticky → a monitored series stays monitored.
    assert _act(True, 20, floor=20, promote=35) == "hold"
    assert _act(True, 34, floor=20, promote=35) == "hold"


def test_dwell_delays_unmonitor():
    # below the floor but not yet below it long enough → hold (still clocking).
    assert _act(True, 10, floor=20, dwell=7, age_days=3) == "hold"
    assert _act(True, 10, floor=20, dwell=7, age_days=7) == "unmonitor"


def test_keep_tagged_or_watched_never_unmonitors():
    # a pinned or watched series stays monitored however low it scores — the hard guard.
    assert _act(True, 5, keep_tagged=True) == "hold"
    assert _act(True, 5, watched=True) == "hold"
    # ...but the guard does NOT force-monitor a series the user left unmonitored.
    assert _act(False, 5, keep_tagged=True) == "hold"
    assert _act(False, 5, watched=True) == "hold"


def test_unmonitored_promotes_only_at_threshold():
    assert _act(False, 35, promote=35) == "monitor"     # climbed back to the bar
    assert _act(False, 80, promote=35) == "monitor"
    assert _act(False, 34, promote=35) == "hold"        # below the promote bar → stay dormant


def test_hysteresis_band_is_sticky_both_ways():
    # a series at 25 (in the [20,35) band): monitored stays monitored, unmonitored stays unmonitored.
    assert _act(True, 25, floor=20, promote=35) == "hold"
    assert _act(False, 25, floor=20, promote=35) == "hold"
