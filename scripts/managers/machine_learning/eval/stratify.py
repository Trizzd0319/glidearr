"""eval/stratify.py — popularity stratification for de-biased offline evaluation.
==============================================================================
Phase 0 of DESIGN_recommendation_enhancement.md. Offline recommender metrics carry a
strong popularity/selection bias (Castells & Moffat 2022): a ranker is rewarded for
parroting what's popular, and recommendation amplifies that. To see past it, segment
test items into head / torso / tail by interaction mass and report metrics PER
SEGMENT — a re-ranker that only wins on the head hasn't learned household taste.

PURE: stdlib only (brain-purity safe).

Segmentation is by interaction MASS, not item count: ``head`` is the set of most-
popular items that together account for the top ``head_frac`` of all interactions,
``tail`` the items making up the bottom ``tail_frac``, ``torso`` the middle.
"""
from __future__ import annotations

from collections import Counter
from typing import Hashable, Iterable, Mapping, Sequence

Item = Hashable


def popularity_counts(events: Sequence[Mapping], *, item_key: str = "item") -> Counter:
    """Interaction count per item across the (training) events."""
    return Counter(e[item_key] for e in events)


def popularity_segments(
    counts: Mapping[Item, int], *, head_frac: float = 0.2, tail_frac: float = 0.2
) -> dict[Item, str]:
    """Map each item → 'head' | 'torso' | 'tail' by cumulative interaction mass.

    Items are walked most-popular first; an item is 'head' while cumulative mass
    before it is under ``head_frac`` of the total, 'tail' once it is past
    ``1-tail_frac``, else 'torso'. Ties broken by stringified id for determinism."""
    total = sum(counts.values())
    if total <= 0:
        return {}
    items = sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))
    head_cut = head_frac * total
    tail_cut = (1.0 - tail_frac) * total
    seg: dict[Item, str] = {}
    cum = 0
    for it, c in items:
        if cum < head_cut:
            seg[it] = "head"
        elif cum < tail_cut:
            seg[it] = "torso"
        else:
            seg[it] = "tail"
        cum += c
    return seg


def segment_of(item: Item, segments: Mapping[Item, str], *, default: str = "tail") -> str:
    """Segment label for an item; items unseen in training default to 'tail' (cold/rare)."""
    return segments.get(item, default)


def relevant_by_segment(
    relevant: Iterable[Item], segments: Mapping[Item, str], *, default: str = "tail"
) -> dict[str, set]:
    """Partition a query's relevant items into {segment: {items}} so per-segment
    recall/NDCG can be computed against the same ranked list."""
    out: dict[str, set] = {"head": set(), "torso": set(), "tail": set()}
    for it in relevant:
        out.setdefault(segment_of(it, segments, default=default), set()).add(it)
    return out
