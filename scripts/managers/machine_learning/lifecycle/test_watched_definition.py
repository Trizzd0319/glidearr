"""Tests for the GLOBAL watched bar — lifecycle.watched_definition.

The knob's resolution order (new system-level key → the legacy
``episode_retention`` alias → 85), the per-play verdict and its three explicitly
handled input shapes, and the two invariants that make the change safe:

  * a sub-threshold play does NOT count as watched, but STILL registers as
    started/abandoned in ``watch_likelihood`` — it must never fall through to the
    UNTOUCHED branch, where abandoning a title would RAISE its quality target;
  * the retention rule and the parquet's ``is_watched`` resolve the SAME bar, so
    the two aggregations can never admit different plays.
"""
from __future__ import annotations

from scripts.managers.machine_learning.likelihood.watch_likelihood import (
    explain_likelihood,
)
from scripts.managers.machine_learning.lifecycle.viewer_retention import (
    resolve_retention_config,
)
from scripts.managers.machine_learning.lifecycle.viewer_retention import (
    watched_by_tautulli as _reexported,
)
from scripts.managers.machine_learning.lifecycle.watched_definition import (
    DEFAULT_WATCHED_PERCENT,
    play_is_watched,
    resolve_watched_percent,
    watched_by_tautulli,
)


# ── the knob ──────────────────────────────────────────────────────────────────
def test_default_is_tautullis_own_85():
    assert DEFAULT_WATCHED_PERCENT == 85.0
    assert resolve_watched_percent({}) == 85.0
    assert resolve_watched_percent(None) == 85.0


def test_new_system_level_key_beats_the_legacy_episode_retention_alias():
    """The knob was born as ``episode_retention.watched_percent`` when it governed
    the retention rule alone. It now governs ``is_watched`` for the whole library
    (movies included), so the system-level key is authoritative — but the old one
    keeps working, because a live config.json already carries it."""
    assert resolve_watched_percent({"watched_threshold": {"percent": 70}}) == 70.0
    assert resolve_watched_percent({"episode_retention": {"watched_percent": 90}}) == 90.0
    assert resolve_watched_percent({
        "watched_threshold": {"percent": 70},
        "episode_retention": {"watched_percent": 90},
    }) == 70.0


def test_knob_is_clamped_and_falls_through_on_junk_rather_than_to_zero():
    """A bar of 0 silently restores the bug (every play counts), so an unparseable
    value must fall through to the next source, never collapse to 0."""
    assert resolve_watched_percent({"watched_threshold": {"percent": 250}}) == 100.0
    assert resolve_watched_percent({"watched_threshold": {"percent": -5}}) == 0.0
    assert resolve_watched_percent({"watched_threshold": {"percent": "nonsense"},
                                    "episode_retention": {"watched_percent": 60}}) == 60.0
    assert resolve_watched_percent({"watched_threshold": {"percent": None}}) == 85.0
    assert resolve_watched_percent({"watched_threshold": "not-a-dict"}) == 85.0


def test_retention_rule_and_the_global_bar_resolve_the_same_number():
    """Not double-applied and not differently-thresholded: one knob, both readers."""
    for cfg in ({}, {"watched_threshold": {"percent": 70}},
                {"episode_retention": {"watched_percent": 60}},
                {"watched_threshold": {"percent": 70},
                 "episode_retention": {"watched_percent": 60}}):
        assert resolve_retention_config(cfg)["watched_percent"] == resolve_watched_percent(cfg)


# ── the per-play verdict ──────────────────────────────────────────────────────
def test_tautullis_own_verdict_beats_the_percentage_fallback():
    """``watched_status`` encodes the threshold the OPERATOR configured in Tautulli
    (shared with Plex), so it wins in BOTH directions — that is the whole point of
    being 'consistent with what Plex and Tautulli dictate'."""
    # verdict says watched, percentage would have said no
    assert play_is_watched({"watched_status": 1, "percent_complete": 12},
                           threshold_pct=85) is True
    assert play_is_watched({"watched_status": "1", "percent_complete": 0},
                           threshold_pct=85) is True
    # verdict says NOT watched, percentage would have said yes
    assert play_is_watched({"watched_status": 0.5, "percent_complete": 99},
                           threshold_pct=85) is False
    assert play_is_watched({"watched_status": 0, "percent_complete": 100},
                           threshold_pct=85) is False


def test_percentage_fallback_when_watched_status_is_absent():
    """Rows cached before ``watched_status`` was admitted to the Tautulli projection
    whitelist carry ``percent_complete`` — so the transition is silent, not a mass
    un-watching. The live cache is entirely in this state today."""
    assert play_is_watched({"percent_complete": 85}, threshold_pct=85) is True
    assert play_is_watched({"percent_complete": 84.9}, threshold_pct=85) is False
    assert play_is_watched({"percent_complete": 2}, threshold_pct=85) is False
    assert watched_by_tautulli(None, 90, threshold_pct=85) is True


def test_a_row_lacking_BOTH_fields_is_handled_explicitly_and_fails_OPEN():
    """No verdict and no percentage is 'we know nothing', not 'unwatched'. This
    predicate gates DELETE guards (the grace clock, a viewer's protected interval);
    a guard must never shrink on missing data."""
    assert play_is_watched({}, threshold_pct=85) is True
    assert play_is_watched({"watched_status": None, "percent_complete": None},
                           threshold_pct=85) is True
    assert play_is_watched({"percent_complete": ""}, threshold_pct=85) is True
    assert play_is_watched(None, threshold_pct=85) is True          # not even a dict
    assert play_is_watched("not-a-row", threshold_pct=85) is True


def test_watched_by_tautulli_is_re_exported_from_viewer_retention():
    """Its original import path still works — several callers and tests use it."""
    assert _reexported is watched_by_tautulli


# ── THE interaction: sub-threshold plays keep their partial-view signal ───────
def _L(row):
    return explain_likelihood(row, config={})


def test_sub_threshold_play_is_not_watched_but_IS_still_started_or_abandoned():
    """The risk this change had to clear: if a sample stopped counting as watched
    AND lost its partial-view signal, it would land on the UNTOUCHED branch
    (untouched_base 25 + score) — so abandoning a show could RAISE its quality
    target. ``percent_complete`` is deliberately NOT thresholded, so it does not."""
    started = _L({"watch_count": 0, "is_watched": False, "percent_complete": 55,
                  "watchability_score": 40})
    assert started["engagement_tier"] == "started"
    assert started["engagement"] == 45.0

    abandoned = _L({"watch_count": 0, "is_watched": False, "percent_complete": 15,
                    "watchability_score": 40})
    assert abandoned["engagement_tier"] == "abandoned"
    # The ceiling BINDS: a 40-score title would otherwise reach 25+40 = 65.
    assert abandoned["likelihood"] == 25.0


def test_abandoning_a_title_can_never_RAISE_its_likelihood():
    """The inversion, stated directly: for every completion band, the sub-threshold
    result must be <= the result the same row got when a play counted as a watch."""
    for pct in (0, 1, 5, 15, 19, 20, 50, 84):
        before = _L({"watch_count": 1, "is_watched": True, "percent_complete": pct,
                     "watchability_score": 45, "last_watched_at": "2026-07-01T00:00:00+00:00"})
        after = _L({"watch_count": 0, "is_watched": False, "percent_complete": pct,
                    "watchability_score": 45, "last_watched_at": "2026-07-01T00:00:00+00:00"})
        assert after["likelihood"] <= before["likelihood"], pct
        assert after["engagement_tier"] != "untouched", pct


def test_a_play_reported_at_zero_percent_is_abandoned_not_untouched():
    """``percent_complete`` cannot express 'tried it, Tautulli logged 0%'. The
    threshold-free ``last_watched_at`` carries that bit — two live episode rows and
    one live movie row are in exactly this state."""
    tried = _L({"watch_count": 0, "is_watched": False, "percent_complete": 0,
                "watchability_score": 45, "last_watched_at": "2026-07-05T03:03:38+00:00"})
    assert tried["engagement_tier"] == "abandoned"
    assert tried["likelihood"] == 25.0
    never = _L({"watch_count": 0, "is_watched": False, "percent_complete": 0,
                "watchability_score": 45, "last_watched_at": None})
    assert never["engagement_tier"] == "untouched"
    assert never["likelihood"] == 70.0                     # untouched_base 25 + score 45


def test_percent_complete_is_read_as_0_100_not_sniffed_as_a_fraction():
    """A PARQUET row at 1% complete is 1%, not 100%. The old magnitude sniff
    (``0 < v <= 1 -> x100``) read it as fully watched; that was masked while any
    play forced the watched branch, and six live movie rows sit at exactly 1."""
    one_pct = _L({"watch_count": 0, "is_watched": False, "percent_complete": 1,
                  "watchability_score": 10, "last_watched_at": "2026-07-01T00:00:00+00:00"})
    assert one_pct["engagement_tier"] == "abandoned"


def test_completion_pct_keeps_its_0_1_fraction_contract():
    """``completion_pct`` is the movie_scorer schema and IS a fraction — unchanged."""
    assert _L({"watch_count": 0, "completion_pct": 1.0})["engagement_tier"] == "watched"
    assert _L({"watch_count": 0, "completion_pct": 0.5})["engagement_tier"] == "started"
    assert _L({"watch_count": 0, "completion_pct": 0.1})["engagement_tier"] == "abandoned"
    assert _L({"watch_count": 0, "completion_pct": 0.0})["engagement_tier"] == "untouched"


def test_a_qualifying_play_still_gets_the_full_watched_floor():
    """The existing watched path is untouched: one WATCH still floors at 50, and the
    graded rewatch ladder still climbs 50/64/78/90."""
    assert _L({"watch_count": 1, "is_watched": True, "percent_complete": 100})["likelihood"] == 50.0
    assert _L({"watch_count": 2, "is_watched": True})["likelihood"] == 64.0
    assert _L({"watch_count": 3, "is_watched": True})["likelihood"] == 78.0
    assert _L({"watch_count": 9, "is_watched": True})["likelihood"] == 90.0
