"""Tests for lifecycle.grace_policy — the pure decision slices of the Radarr+Sonarr
grace-period marking (ML Step 8, the oracle-mover). The service keeps the per-row df
reads/writes and the guard precomputes; these cover the extracted cores: the window
computation and the per-row precedence.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.managers.machine_learning.lifecycle.grace_policy import (
    episode_grace_decision,
    grace_mark,
    grace_window_multiplier,
    movie_grace_decision,
)

_NOW = datetime(2026, 6, 10, 12, tzinfo=timezone.utc)
_GRACE = timedelta(hours=3)


# ── grace_mark ──────────────────────────────────────────────────────────────────
def test_grace_mark_window_expired_and_live():
    # watched 4h ago + 3h grace -> window closed 1h ago -> marked
    au, marked = grace_mark("2026-06-10T08:00:00Z", _GRACE, _NOW)
    assert au == "2026-06-10T11:00:00+00:00" and marked is True
    # watched 1h ago + 3h grace -> 2h left -> not marked
    _, marked2 = grace_mark("2026-06-10T11:00:00Z", _GRACE, _NOW)
    assert marked2 is False


def test_grace_mark_unparseable_returns_none():
    assert grace_mark("not-a-date", _GRACE, _NOW) == (None, None)
    assert grace_mark(None, _GRACE, _NOW) == (None, None)


# ── grace_window_multiplier (score-scaled window) ───────────────────────────────
def test_grace_window_multiplier_off_is_exactly_one():
    for pct in (None, float("nan"), 0, 50, 100, "bad"):
        m = grace_window_multiplier(pct, {})
        assert m == 1.0 and type(m) is float
    # enabled:false disables even with mults present
    assert grace_window_multiplier(0, {"enabled": False, "low_mult": 0.5, "high_mult": 1.5}) == 1.0
    # grace_td * 1.0 is the exact same window (byte-identical parity)
    assert _GRACE * grace_window_multiplier(0, {}) == _GRACE


def test_grace_window_multiplier_interpolates_and_clamps():
    r = {"enabled": True, "low_mult": 0.5, "high_mult": 1.5}
    assert grace_window_multiplier(0, r) == 0.5
    assert grace_window_multiplier(100, r) == 1.5
    assert grace_window_multiplier(50, r) == 1.0
    assert grace_window_multiplier(150, r) == 1.5    # clamp high
    assert grace_window_multiplier(None, r) == 1.0   # null percentile -> neutral


# ── movie_grace_decision (Radarr) ───────────────────────────────────────────────
def _mv(**kw):
    base = dict(is_franchise_entry=False, fid_franchise_protected=False,
                keep_protected=False, is_watched=True, has_last_watched=True)
    base.update(kw)
    return movie_grace_decision(**base)


def test_movie_grace_decision():
    assert _mv(is_franchise_entry=True, is_watched=False) == "clear"   # franchise beats watched
    assert _mv(fid_franchise_protected=True) == "clear"
    assert _mv(keep_protected=True) == "clear"
    assert _mv(is_watched=False) == "skip"
    assert _mv(has_last_watched=False) == "skip"
    assert _mv() == "mark"


# ── episode_grace_decision (Sonarr) ─────────────────────────────────────────────
def _ep(**kw):
    base = dict(is_pilot=False, is_next=False, is_watched=True, has_last_watched=True,
                fid_protected=False, keep_series=False, keep_season_current=False,
                recent_aired=False, household_blocked=False)
    base.update(kw)
    return episode_grace_decision(**base)


def test_episode_grace_decision_clears_and_skips():
    assert _ep(is_pilot=True, is_watched=False) == "clear"   # pilot cleared even when unwatched
    assert _ep(is_next=True) == "clear"
    assert _ep(is_watched=False) == "skip"                   # (not pilot/next) unwatched -> skip
    assert _ep(has_last_watched=False) == "skip"
    assert _ep(fid_protected=True) == "clear"
    assert _ep(keep_series=True) == "clear"
    assert _ep(keep_season_current=True) == "clear"
    assert _ep(recent_aired=True) == "clear"
    assert _ep(household_blocked=True) == "clear"


def test_episode_grace_decision_marks_when_unguarded():
    assert _ep() == "mark"


def test_episode_grace_decision_precedence_pilot_over_unwatched():
    # pilot/next is checked BEFORE the watched/last-watched skips
    assert _ep(is_pilot=True, is_watched=False, has_last_watched=False) == "clear"
