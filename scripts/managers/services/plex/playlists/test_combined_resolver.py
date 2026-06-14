"""Tests for combined_resolver.build_combined_plan — cross-medium merge + per-medium order."""
from __future__ import annotations

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
