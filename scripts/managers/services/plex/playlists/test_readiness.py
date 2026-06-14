"""Tests for TV-playlist readiness diagnosis — every degraded outcome is messaged."""
from __future__ import annotations

from scripts.managers.services.plex.playlists.readiness import (
    diagnose_tv_readiness,
    headline,
)


def _d(**over):
    base = dict(inventory_present=True, resolution_pct=100.0, max_pages_hit=False,
               series_total=10, series_scored=10, daemon_enabled=True, daemon_running=False)
    base.update(over)
    return diagnose_tv_readiness(**base)


def _codes(diag):
    return {n["code"] for n in diag["notes"]}


# ── the happy path ─────────────────────────────────────────────────────────────
def test_fully_ready_has_no_notes():
    d = _d()
    assert d["can_build"] is True and d["notes"] == []
    assert "ready" in headline(d).lower()


# ── no inventory: cannot build, actionable message ─────────────────────────────
def test_no_inventory_blocks_with_actionable_message():
    d = _d(inventory_present=False)
    assert d["can_build"] is False and _codes(d) == {"no_inventory"}
    assert "plex.episodes.enabled" in headline(d)


# ── coverage tiers ──────────────────────────────────────────────────────────────
def test_low_coverage_warns_but_still_builds():
    d = _d(resolution_pct=55.0)
    assert d["can_build"] is True and _codes(d) == {"low_coverage"}
    assert d["notes"][0]["level"] == "warn" and "legacy Plex TV agent" in d["notes"][0]["message"]


def test_partial_coverage_is_info_only():
    d = _d(resolution_pct=90.0)
    assert _codes(d) == {"partial_coverage"} and d["notes"][0]["level"] == "info"


def test_full_coverage_no_coverage_note():
    assert _codes(_d(resolution_pct=100.0)) == set()


def test_scan_truncation_warns():
    assert "scan_truncated" in _codes(_d(max_pages_hit=True))


# ── enrichment messaging (the key requirement) ─────────────────────────────────
def test_scores_pending_with_daemon_says_it_auto_resumes():
    d = _d(series_scored=2, daemon_enabled=True, daemon_running=True)
    msg = next(n["message"] for n in d["notes"] if n["code"] == "scores_pending_enrichment")
    assert "running now" in msg and "AUTOMATICALLY resume" in msg and "air date" in msg


def test_scores_pending_daemon_enabled_not_running_still_reassures():
    d = _d(series_scored=2, daemon_enabled=True, daemon_running=False)
    msg = next(n["message"] for n in d["notes"] if n["code"] == "scores_pending_enrichment")
    assert "running now" not in msg and "AUTOMATICALLY resume" in msg


def test_scores_pending_without_daemon_suggests_enabling_it():
    d = _d(series_scored=2, daemon_enabled=False)
    assert "scores_no_daemon" in _codes(d)
    assert "enable the enrichment daemon" in next(
        n["message"] for n in d["notes"] if n["code"] == "scores_no_daemon").lower()


def test_mostly_scored_has_no_enrichment_note():
    # 8/10 scored → only 20% unscored, below the pending fraction → no note
    assert _codes(_d(series_scored=8)) == set()


# ── combined degradations stack, and headline prefers a warn ───────────────────
def test_multiple_notes_stack_and_headline_prefers_warn():
    d = _d(resolution_pct=50.0, series_scored=0, daemon_enabled=True)
    assert _codes(d) == {"low_coverage", "scores_pending_enrichment"}
    assert "match a Plex item" in headline(d)        # the warn wins the one-liner
