"""Tests for combined_resolver.build_combined_plan — cross-medium merge + per-medium order."""
from __future__ import annotations

from datetime import date, timedelta

from scripts.managers.machine_learning.playlists.models import PlaylistInput
from scripts.managers.services.plex.playlists.combined_resolver import build_combined_plan


def test_merges_and_normalizes_per_medium():
    movies = [PlaylistInput(rating_key="m_hi", medium="movie", title="MHi", score=90),
              PlaylistInput(rating_key="m_lo", medium="movie", title="MLo", score=10)]
    eps = [PlaylistInput(rating_key="e_hi", medium="episode", title="EHi", score=70, series_id=1),
           PlaylistInput(rating_key="e_lo", medium="episode", title="ELo", score=5, series_id=2)]
    plan, stats = build_combined_plan([movies, eps], max_items=10)
    rks = [i.rating_key for i in plan.items]
    assert rks.index("m_hi") < rks.index("m_lo")              # top-percentile movie leads its medium
    assert rks.index("e_hi") < rks.index("e_lo")              # top-percentile episode leads its medium
    assert stats["by_medium"] == {"movie": 2, "episode": 2} and stats["in_plan"] == 4


def test_watched_dropped_across_mediums():
    movies = [PlaylistInput(rating_key="m", medium="movie", title="M", score=50, watched=True)]
    eps = [PlaylistInput(rating_key="e", medium="episode", title="E", score=50, series_id=1)]
    plan, _ = build_combined_plan([movies, eps], max_items=10)
    assert [i.rating_key for i in plan.items] == ["e"]       # watched movie dropped


def test_empty_inputs_safe():
    plan, stats = build_combined_plan([[], []])
    assert plan.items == () and stats["in_plan"] == 0


def test_recency_boost_threads_through_and_lifts_a_caught_up_fresh_item():
    # build_combined_plan must pass recency_boost/window_days into order_items: a caught-up, freshly
    # acquired item (modest score) leads when on; the higher-watchability stale one leads when off.
    fresh = (date.today() - timedelta(days=5)).isoformat()          # within the 30-day window
    items = [PlaylistInput(rating_key="fresh", medium="movie", title="Fresh", score=30, added_at=fresh),
             PlaylistInput(rating_key="stale_hi", medium="movie", title="StaleHi", score=95,
                           release_date="2001-01-01")]
    off, _ = build_combined_plan([items])
    on, _ = build_combined_plan([items], recency_boost=True, window_days=30)
    assert [i.rating_key for i in off.items] == ["stale_hi", "fresh"]   # watchability rules off
    assert [i.rating_key for i in on.items] == ["fresh", "stale_hi"]    # recency boost flips it
