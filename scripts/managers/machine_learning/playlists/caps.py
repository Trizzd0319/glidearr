"""
playlists/caps.py — group-atomic size cap (keeps the top-ranked prefix whole).
================================================================================
A whole-library playlist can be huge; Plex doesn't paginate it for the user, so a
render must stay scannable. We cap by ITEM count but never split a group: we take
whole groups in ranked order until the next won't fit, then stop (so exclusion never
silently reorders — a smaller lower-ranked group is NOT promoted past a big
higher-ranked one). The one exception is a single group larger than the entire cap,
which we truncate within (better than an empty playlist); the dropped count is
always returned so truncation is observable, never silent.
"""
from __future__ import annotations

from scripts.managers.machine_learning.playlists.models import PlaylistInput


def apply_size_cap(blocks: list[list[PlaylistInput]], max_items: int | None):
    """``blocks`` are groups already in final ranked order. Returns
    (kept_items_flat, truncated_count). ``kept`` is always a prefix of the flattened
    blocks, so the caller can align it with per-group metadata by slicing."""
    flat = [it for b in blocks for it in b]
    if not max_items or max_items <= 0 or len(flat) <= max_items:
        return flat, 0
    kept: list[PlaylistInput] = []
    for block in blocks:
        if len(kept) + len(block) <= max_items:
            kept.extend(block)
        else:
            break
    if not kept:                       # first (top-ranked) group alone exceeds the cap
        kept = flat[:max_items]        # truncate within it rather than emit nothing
    return kept, len(flat) - len(kept)
