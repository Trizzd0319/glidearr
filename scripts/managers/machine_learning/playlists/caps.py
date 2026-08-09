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


def limit_per_group(blocks: list[list[PlaylistInput]], max_per_group: int | None):
    """Trim each group to its first ``max_per_group`` members. Returns
    ``(trimmed_blocks, dropped_count)``.

    APPLIED BEFORE :func:`apply_size_cap`, and the order matters: trimming first
    means the size cap then budgets against the trimmed groups, so a formerly
    oversized group can now FIT rather than being skipped whole. Cap first and
    trim second would waste the budget on members about to be discarded.

    TAKES THE FIRST N, never a sample. ``blocks`` arrive in spoiler-safe
    (season, episode) order, so the first member is the one the viewer must see
    next; any other choice would offer S01E05 while S01E01 is unwatched.

    WHY THIS EXISTS. "Touch & Go" is the STANDALONE list - low-commitment
    one-offs - and it shipped with five consecutive Suits episodes at positions
    3-7. Group contiguity is correct behaviour for Up Next, where following one
    show is the point; for a one-offs list it defeats the entire premise. The
    difference is a per-FAMILY policy, not a bug in the ordering.

    ``None`` or a non-positive value is a no-op, so a family that has not opted
    in behaves exactly as before.
    """
    if not max_per_group or max_per_group <= 0:
        return blocks, 0
    n = int(max_per_group)
    trimmed = [b[:n] for b in blocks]
    dropped = sum(len(b) for b in blocks) - sum(len(b) for b in trimmed)
    return trimmed, dropped


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
