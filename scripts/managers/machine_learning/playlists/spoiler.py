"""
playlists/spoiler.py — the spoiler-safety invariant (verification, not mutation).
================================================================================
The contract every ordering must satisfy: walking the final playlist top-to-bottom,
a viewer never reaches a later episode of a series before an earlier UNWATCHED one
of that same series. Because ``order_items`` drops watched items and orders each
series by (season, episode), the surviving episodes are ascending — this module
asserts that property so it can be pinned by property tests and (cheaply) re-checked
at runtime before a write.

Specials (season 0 / ``is_special``) are exempt — they legitimately sit at a track
tail and carry no "must precede" relationship.
"""
from __future__ import annotations

from scripts.managers.machine_learning.playlists.models import PlaylistInput


def is_spoiler_safe(ordered: list[PlaylistInput]) -> bool:
    """True iff, for every series, the non-special episodes appear in non-decreasing
    (season, episode) order in ``ordered``."""
    last: dict[int, tuple[int, int]] = {}
    for it in ordered:
        if it.medium != "episode" or it.series_id is None:
            continue
        if it.is_special or it.season == 0:
            continue
        if it.season is None or it.episode is None:
            continue
        key = (it.season, it.episode)
        prev = last.get(it.series_id)
        if prev is not None and key < prev:
            return False
        last[it.series_id] = key
    return True
