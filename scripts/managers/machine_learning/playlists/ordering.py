"""
playlists/ordering.py — THE crown-jewel ordering (Hard-req #4).
================================================================================
Pipeline (pure, deterministic):
  1. Drop items the user already watched (Hard-req #5 per-user filter).
  2. GROUP by connected components over shared series/franchise/universe (grouping.py)
     so co-affiliated items stay contiguous.
  3. Order WITHIN each group by spoiler-safe timeline (timeline.py).
  4. Rank GROUPS by watchability — a group is as compelling as its strongest still-
     watchable entry — with optional per-medium percentile normalization so a movie
     score and a (different-scorer) show score are comparable across groups.
  5. Apply the group-atomic size cap (caps.py).
  6. Emit ``PlaylistItemPlan``s with ordinal + rationale and a coverage stat.

Every tie ends in a deterministic key (size, lead chrono, title, group/rating key),
so a golden corpus pins the exact order regardless of input order.
"""
from __future__ import annotations

from scripts.managers.machine_learning.playlists.caps import apply_size_cap
from scripts.managers.machine_learning.playlists.grouping import coverage_stats, group_items
from scripts.managers.machine_learning.playlists.models import (
    STANDALONE,
    PlaylistInput,
    PlaylistItemPlan,
    PlaylistPlan,
)
from scripts.managers.machine_learning.playlists.timeline import chrono_value, order_within_group

_NEG_INF = float("-inf")


def _medium_percentile(items: list[PlaylistInput]) -> dict[str, float]:
    """rating_key → percentile (0,1] of its score WITHIN its medium. Lets a movie
    score and a show score (computed by different scorers on different signal mixes)
    be ranked on one comparable axis for cross-group ordering. Items with no score
    are omitted (resolver returns None for them)."""
    by_medium: dict[str, list[float]] = {}
    for it in items:
        if it.score is not None:
            by_medium.setdefault(it.medium, []).append(it.score)
    for scores in by_medium.values():
        scores.sort()
    out: dict[str, float] = {}
    for it in items:
        if it.score is None:
            continue
        scores = by_medium[it.medium]
        # fraction of same-medium items at-or-below this score → (0, 1]
        lo, hi = 0, len(scores)
        while lo < hi:                     # rightmost index where scores[i] <= score
            mid = (lo + hi) // 2
            if scores[mid] <= it.score:
                lo = mid + 1
            else:
                hi = mid
        out[it.rating_key] = lo / len(scores)
    return out


def _score_resolver(items: list[PlaylistInput], normalize_per_medium: bool):
    if not normalize_per_medium:
        return lambda it: it.score
    pct = _medium_percentile(items)
    return lambda it: pct.get(it.rating_key)


def order_items(items: list[PlaylistInput], *, family: str = "up_next",
                max_items: int | None = None, normalize_per_medium: bool = False,
                include_specials: bool = False) -> PlaylistPlan:
    """Order candidate items into a spoiler-safe, group-contiguous, watchability-ranked
    :class:`PlaylistPlan`. See module docstring for the pipeline."""
    items = list(items)
    considered = len(items)

    live = [it for it in items if not it.watched]
    dropped_watched = considered - len(live)
    if not include_specials:
        live = [it for it in live if not (it.is_special or it.season == 0)]

    if not live:
        return PlaylistPlan(family=family, items=(), considered=considered,
                            dropped_watched=dropped_watched, truncated=0, coverage={})

    groups = group_items(live)
    coverage = coverage_stats(groups)
    score_of = _score_resolver(live, normalize_per_medium)

    # order within each group, compute the group's ranking score (max over members)
    rendered = []
    for g in groups:
        members = order_within_group(list(g.members))
        scores = [score_of(m) for m in members if score_of(m) is not None]
        top = max(scores) if scores else _NEG_INF
        rendered.append((g, members, top))

    # rank groups: watchability DESC, then size DESC, then earliest lead date,
    # then lead title, then group key — fully deterministic.
    def _rank(entry):
        g, members, top = entry
        lead = members[0]
        return (-top, -len(members), chrono_value(lead), lead.title.casefold(), g.key)

    rendered.sort(key=_rank)

    # flatten in ranked order, carrying group identity + rank score per item
    flat: list[tuple[PlaylistInput, object, float, int, int]] = []
    for g, members, top in rendered:
        n = len(members)
        for idx, m in enumerate(members):
            flat.append((m, g, top, idx, n))

    blocks = [members for _, members, _ in rendered]
    kept, truncated = apply_size_cap(blocks, max_items)
    flat = flat[:len(kept)]                 # kept is a prefix of the flattened blocks

    plans = []
    for ordinal, (m, g, top, idx, n) in enumerate(flat):
        score = None if top == _NEG_INF else top
        if g.kind == STANDALONE:
            reason = f"watchability {score:.0f}" if score is not None else "owned"
        else:
            reason = f"{g.kind} '{g.key}' · {idx + 1}/{n}"
        plans.append(PlaylistItemPlan(
            rating_key=m.rating_key, ordinal=ordinal, group_key=g.key,
            group_kind=g.kind, score=score, reason=reason))

    return PlaylistPlan(
        family=family, items=tuple(plans), considered=considered,
        dropped_watched=dropped_watched, truncated=truncated, coverage=coverage)
