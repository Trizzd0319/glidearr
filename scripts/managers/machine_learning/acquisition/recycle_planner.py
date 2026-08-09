"""
recycle_planner.py — self-funding ("leapfrog") episode acquisition.
================================================================================
Pure decision core. Given a series' OWNED+WATCHED episodes and the episodes the
prefetch WANTS next, decide which consumed episodes to recycle so the acquisition
pays for itself. No I/O, no config reads, no logger — the caller supplies numbers
and applies the plan.

THE PROBLEM THIS SOLVES. The next-episode prefetch already picks the right episodes
(``recency_gate`` walks the most-recently-watched series first), and space pressure
then vetoes them wholesale:

    Acquisition skipped for 'standard': 924.9 GB free < 5500 GB band top
    (in the space-pressure band). 9 episode(s) remain queued.

So a library that is ~93% pilot-only can never get ahead: the episodes that would
replace those pilots are never allowed in. The household watches Fallout S1E6 and
"Up Next" offers it ``Fear the Walking Dead S1E1``, because S1E7 was never acquired.

THE INSIGHT. An acquisition funded by deleting an episode the household has ALREADY
WATCHED is not the same act as reclaiming space under pressure. Nothing is lost: the
episode was consumed, the series stays, the library does not grow. Net space change
is <= 0, so the floor the gate protects is never breached. A rolling window — watch
one, recycle it, acquire the next — is how a stay-ahead library is supposed to work.

WHY THIS IS NOT `deletions_consent`. That flag answers *"the disk is full, may I give
something up?"*. This answers *"I finished this episode, may I spend it on the next
one?"*. An operator can reasonably want the second with the first firmly off, and
conflating them makes that impossible (precedent: ``relocation_consent`` is separate
for the same reason). The caller must gate on its own flag.

THE GUARDS, and the specific failure each prevents:

  1. SAME SERIES ONLY        — funding Fallout by deleting Blue Bloods is a
                               reallocation decision. That is space_pressure's job.
                               This function only ever sees one series.
  2. HOUSEHOLD-WATCHED ONLY  — not per-user. Trizzd finishing S1E5 does not mean Mom
                               has. Recycling on one viewer's progress would delete
                               an episode another member is walking toward.
  3. REWATCH BUFFER          — the N most recently watched are kept. Someone
                               mid-binge may step back an episode, and the newest
                               watch is the likeliest rewatch.
  4. SIZE-MATCHED            — freed >= ratio x acquired, or the net is positive and
                               the whole premise fails.
  5. KEEP-TAGGED NEVER       — a pinned series is not a rotation candidate, watched
                               or not.

ORDERING: DELETE FIRST, THEN ACQUIRE. The inverse of the 4K path's make-before-break,
and correct here for the opposite reason. There, deleting a 4K copy before its 1080p
baseline lands risks losing the title outright. Here the episode is ALREADY CONSUMED,
so losing it costs nothing — while acquiring first would breach the floor the gate
exists to protect. The plan is emitted in that order and must be applied in it.
"""
from __future__ import annotations

DEFAULT_REWATCH_BUFFER = 2      # keep the N most-recently-watched episodes
DEFAULT_SIZE_TOLERANCE = 0.4    # an acquisition may cost up to 1.4x what it recycles


def _tiers_of(want) -> list:
    """``[(resolution, est_gb), ...]`` for a wanted episode, best tier first.

    Accepts either ``est_gb_by_tier`` (``{2160: 8.0, 1080: 3.2, 720: 1.4}``) or a bare
    ``est_gb``, which is treated as a single unlabelled tier. Sorted by resolution
    DESCENDING so the funding loop always offers the best quality that fits.
    """
    by_tier = (want or {}).get("est_gb_by_tier")
    if isinstance(by_tier, dict) and by_tier:
        out = []
        for res, gb in by_tier.items():
            try:
                g = float(gb or 0.0)
                if g > 0:
                    out.append((int(res), g))
            except (TypeError, ValueError):
                continue
        return sorted(out, key=lambda t: -t[0])
    g = _est_of(want)
    return [(0, g)] if g > 0 else []


def plan_recycle(
    *,
    watched_owned: list,
    wanted: list,
    free_gb: float,
    floor_gb: float,
    keep_tagged: bool = False,
    rewatch_buffer: int = DEFAULT_REWATCH_BUFFER,
    size_tolerance: float = DEFAULT_SIZE_TOLERANCE,
) -> dict:
    """Plan a self-funding acquisition for ONE series.

    Args:
        watched_owned: episodes owned AND watched by the household, each
            ``{"season", "episode", "size_gb", "watched_at", "episode_file_id"}``.
            ``watched_at`` should be the HOUSEHOLD watch time (see guard 2); rows
            without one are treated as unwatched and are never recycled.
        wanted: episodes the prefetch wants, in priority order, each
            ``{"season", "episode", "est_gb"}`` or, preferably,
            ``{"season", "episode", "est_gb_by_tier": {2160: .., 1080: .., 720: ..}}``.
        free_gb: current free space.
        floor_gb: the band top the acquisition gate is enforcing.
        keep_tagged: True when the series is pinned — returns an empty plan.
        rewatch_buffer: how many of the most-recently-watched to keep (guard 3).
        size_tolerance: an acquisition may cost up to ``(1 + tolerance)`` x what it
            recycles (guard 4). 0.4 means a next episode within 40% of the recycled
            one's size is allowed through — strict parity would stall a rotation over
            ordinary episode-to-episode variance, which is the failure mode that keeps
            a stay-ahead library stuck on pilots.

    Returns:
        ``{"recycle": [...], "acquire": [...], "freed_gb", "cost_gb", "reason"}``.
        Each ``acquire`` entry carries the ``tier`` chosen and the ``est_gb`` at that
        tier — the caller MUST request at that tier, or the size arithmetic here is
        void. ``recycle`` is emitted in DELETE-FIRST order; an empty ``acquire`` means
        the acquisition stays blocked and nothing should be deleted.

    RESOLUTION TIERING IS PART OF THE FUNDING DECISION, not an afterthought. Under
    pressure the loop offers the BEST tier that the recycled space affords and steps
    down when it does not fit — so recycling a 720p episode funds a 720p replacement,
    never a 2160p one. Without this the rotation would silently UPGRADE quality while
    reporting itself net-neutral, and the disk would grow one episode at a time.

    An empty plan is always safe: the caller falls back to the normal gate, which is
    to skip the acquisition. Nothing here can produce a deletion that is not paying
    for a specific, named acquisition in the same plan.
    """
    empty = {"recycle": [], "acquire": [], "freed_gb": 0.0, "cost_gb": 0.0, "reason": ""}

    if keep_tagged:
        return {**empty, "reason": "keep-tagged: never recycled"}
    if not wanted:
        return {**empty, "reason": "nothing wanted"}
    if float(free_gb) >= float(floor_gb):
        # Not under pressure — the normal path already allows this. Recycling here
        # would delete something for no reason at all.
        return {**empty, "reason": "not under pressure; normal gate applies"}

    # Guard 2 + 3: household-watched only, newest N kept as a rewatch buffer.
    dated = [e for e in (watched_owned or [])
             if e.get("watched_at") and _size_of(e) > 0]
    if not dated:
        return {**empty, "reason": "no household-watched episodes to recycle"}
    dated.sort(key=lambda e: str(e.get("watched_at")))          # oldest watch first
    buffer_n = max(0, int(rewatch_buffer))
    candidates = dated[:-buffer_n] if buffer_n and len(dated) > buffer_n else (
        [] if buffer_n else dated)
    if not candidates:
        return {**empty,
                "reason": f"all {len(dated)} watched episode(s) held by the "
                          f"{buffer_n}-episode rewatch buffer"}

    # ADVANCE AS FAR AS THE WHOLE POOL ALLOWS, THEN DELETE ONLY WHAT IS SPENT.
    #
    # Two passes, and the order matters. The obvious single-pass version -- recycle just
    # enough for each want as you go -- stops at the FIRST tier that fits and therefore
    # never discovers that the rest of the pool could have afforded a better tier or a
    # further episode. It under-uses the pool by construction.
    #
    # So: price the whole advance against the FULL eligible pool first (pass 1), then take
    # only the episodes that advance actually costs (pass 2). Any surplus stays on disk.
    # A recycle that is not paying for a specific acquisition in this plan is a RECLAIM,
    # which is space_pressure's job and a different consent.
    tol = max(0.0, float(size_tolerance))
    available = sum(_size_of(e) for e in candidates)

    # ── pass 1: how far forward can we get? ───────────────────────────────
    acquire = []
    cost = 0.0
    for want in wanted:
        tiers = _tiers_of(want)
        if not tiers:
            continue
        remaining = (available * (1.0 + tol)) - cost
        placed = next(((r, g) for r, g in tiers if g <= remaining), None)
        if placed is None:
            # Cannot afford this one even at its lowest tier. STOP rather than skipping
            # to a cheaper later episode: the prefetch order IS the priority order, and
            # jumping ahead would silently reorder what the household gets next.
            break
        acquire.append({**want, "tier": placed[0], "est_gb": round(placed[1], 2)})
        cost += placed[1]

    if not acquire:
        return {**empty, "reason": "no acquisition could be funded from the watched pool"}

    # ── pass 2: pay for exactly that, oldest watch first ──────────────────────
    recycle = []
    freed = 0.0
    for ep in candidates:
        if freed * (1.0 + tol) >= cost:
            break                      # the advance is paid for; the rest stays on disk
        recycle.append(ep)
        freed += _size_of(ep)

    _tiers = sorted({a.get("tier") for a in acquire if a.get("tier")}, reverse=True)
    return {
        "recycle": recycle,
        "acquire": acquire,
        "freed_gb": round(freed, 2),
        "cost_gb": round(cost, 2),
        "held_gb": round(available - freed, 2),
        "reason": (f"recycling {len(recycle)} of {len(candidates)} eligible watched "
                   f"episode(s) ({freed:.1f} GB) to advance {len(acquire)} episode(s) "
                   f"({cost:.1f} GB"
                   + (f" at {'/'.join(f'{t}p' for t in _tiers)}" if _tiers else "")
                   + f"); {available - freed:.1f} GB of watched content left in place"),
    }


def _size_of(ep) -> float:
    try:
        return max(0.0, float(ep.get("size_gb") or 0.0))
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _est_of(ep) -> float:
    try:
        return max(0.0, float(ep.get("est_gb") or 0.0))
    except (TypeError, ValueError, AttributeError):
        return 0.0
