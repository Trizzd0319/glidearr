"""
features/completion_stats.py — watch-completion derivations (pure).
================================================================================
Relocated from ``services/tautulli/series.get_series_completion_stats`` and
``services/tautulli/episodes.get_episode_completion_stats`` (ML Step 3b). PURE —
process pre-fetched Tautulli history; no HTTP, no logging, no global_cache. The
Tautulli series/episodes managers keep the raw history FETCH + their summary log
and delegate the counting here.

Public API:
  * series_completion_stats(history_entries)
      -> {show_title: {"completed": int, "incomplete": int}}  (episodes >=90% = completed)
  * episode_completion_stats(history_entries)
      -> {"completed": int, "incomplete": int}  (household-wide episode tallies)
"""
from __future__ import annotations


def series_completion_stats(history_entries: list) -> dict:
    """Per-show watched/incomplete episode counts. An episode entry counts as
    completed when ``percent_complete >= 90``. Keyed by ``grandparent_title``
    (missing key defaults to ``"Unknown"``)."""
    series_map: dict = {}
    for entry in history_entries:
        if entry.get("media_type") != "episode":
            continue
        show = entry.get("grandparent_title", "Unknown")
        if show not in series_map:
            series_map[show] = {"completed": 0, "incomplete": 0}
        if entry.get("percent_complete", 0) >= 90:
            series_map[show]["completed"] += 1
        else:
            series_map[show]["incomplete"] += 1
    return series_map


def episode_completion_stats(history_entries: list) -> dict:
    """Household-wide episode completion tally (episodes >=90% = completed)."""
    completed = 0
    incomplete = 0
    for entry in history_entries:
        if entry.get("media_type") != "episode":
            continue
        if entry.get("percent_complete", 0) >= 90:
            completed += 1
        else:
            incomplete += 1
    return {"completed": completed, "incomplete": incomplete}
