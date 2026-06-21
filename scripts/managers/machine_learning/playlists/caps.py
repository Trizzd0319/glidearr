"""
playlists/caps.py — group-atomic size cap (keeps whole groups, never starves).
================================================================================
A whole-library playlist can be huge; Plex doesn't paginate it for the user, so a
render must stay scannable. We cap by ITEM count but never split a group: we walk
groups in ranked order, taking each whole group that still fits and SKIPPING one that
doesn't — then keep filling from the smaller lower-ranked groups behind it. Skipping
(rather than stopping at the first overflow) is deliberate: a single oversized group —
e.g. a 200-member mega-group — must not be able to starve the entire playlist down to
the handful of items that happened to rank ahead of it. Each INCLUDED group stays whole
and contiguous; only the skipped-over groups leave a rank gap. The one remaining
exception is when even the smallest group exceeds the whole cap: we truncate within the
top-ranked group (better than an empty playlist). The dropped count is always returned
so truncation is observable, never silent.
"""
from __future__ import annotations

from scripts.managers.machine_learning.playlists.models import PlaylistInput


def apply_size_cap(blocks: list[list[PlaylistInput]], max_items: int | None):
    """``blocks`` are groups already in final ranked order. Returns
    (kept_items_flat, truncated_count). ``kept`` is the concatenation of the whole
    groups that fit, in rank order — NOT necessarily a prefix of the flattened blocks,
    since an oversized group is skipped and filling continues from smaller ones. The
    caller aligns metadata by item identity, not by slicing."""
    flat = [it for b in blocks for it in b]
    if not max_items or max_items <= 0 or len(flat) <= max_items:
        return flat, 0
    kept: list[PlaylistInput] = []
    for block in blocks:
        if len(kept) + len(block) <= max_items:
            kept.extend(block)        # fits → take the whole group
        # else: skip this group (too big for the remaining budget) and keep filling
        # from smaller lower-ranked groups, so one huge group can't starve the playlist
    if not kept:                       # even the smallest group exceeds the whole cap
        kept = flat[:max_items]        # truncate within the top-ranked group, not empty
    return kept, len(flat) - len(kept)
