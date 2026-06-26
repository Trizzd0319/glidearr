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
  * per_user_platform_usage(history_entries, user_list) -> {username: {platform: count}}
        the per-user variant — which devices each user actually plays on, the input the
        codec-aware selector turns into predict_transcode's per-user platform_weights.

NOTE: the companion transcode-usage derivation lives in
``quality_analytics/transcode.py`` (transcode is a quality/playback concern).
"""
from __future__ import annotations

from collections import defaultdict


def platform_usage(history_entries: list) -> dict:
    """Platform play-count tally from pre-fetched history, sorted descending by
    count. Missing ``platform`` defaults to ``"Unknown"``."""
    platform_map: dict = {}
    for entry in history_entries:
        platform = entry.get("platform", "Unknown")
        platform_map[platform] = platform_map.get(platform, 0) + 1
    return dict(sorted(platform_map.items(), key=lambda x: x[1], reverse=True))


def per_user_platform_usage(history_entries: list, user_list: list) -> dict:
    """``{username: {platform: count}}`` — :func:`platform_usage` run per user. Groups history by
    the stable Tautulli ``user_id`` (falling back to the friendly ``user`` name, then the username),
    exactly mirroring :func:`affinity.genre_affinity.per_user_affinity`'s join so a user's device
    usage and genre affinity always key the same way. Users with no matching history are omitted.
    Pure."""
    by_id: dict = defaultdict(list)
    by_name: dict = defaultdict(list)
    for entry in history_entries:
        uid = str(entry.get("user_id") or "")
        if uid:
            by_id[uid].append(entry)
        name = str(entry.get("user") or "")
        if name:
            by_name[name].append(entry)

    out: dict = {}
    for user in (user_list or []):
        username = str(user.get("username") or user.get("user_id") or "")
        if not username:
            continue
        entries = (
            by_id.get(str(user.get("user_id") or ""))
            or by_name.get(str(user.get("friendly_name") or ""))
            or by_name.get(username)
        )
        if entries:
            out[username] = platform_usage(entries)
    return out
