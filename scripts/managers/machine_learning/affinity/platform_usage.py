"""
affinity/platform_usage.py — device usage signal (pure).
================================================================================
Relocated from ``services/tautulli/devices.get_platform_usage`` (ML Step 3b-2).
Tallies how often each Plex platform appears in the pre-fetched history — the
household's primary-device signal that feeds scoring Group-D (device/playback
fit). PURE — no HTTP, no global_cache, no logging; the devices manager keeps the
raw history FETCH + its summary log and delegates here.

Public API:
  * platform_usage(history_entries) -> {platform_name: play_count}  (desc by count)

NOTE: the companion transcode-usage derivation lives in
``quality_analytics/transcode.py`` (transcode is a quality/playback concern).
"""
from __future__ import annotations


def platform_usage(history_entries: list) -> dict:
    """Platform play-count tally from pre-fetched history, sorted descending by
    count. Missing ``platform`` defaults to ``"Unknown"``."""
    platform_map: dict = {}
    for entry in history_entries:
        platform = entry.get("platform", "Unknown")
        platform_map[platform] = platform_map.get(platform, 0) + 1
    return dict(sorted(platform_map.items(), key=lambda x: x[1], reverse=True))
