"""eval/replay.py — pure reconstruction of pre-cutoff household state.
==============================================================================
Phase 0 of DESIGN_recommendation_enhancement.md, the *honest* (leakage-free) half
of the scorecard baseline. The watchability scorecard reads watch history (Group A
completion/rewatch, Group B affinity, the watched-set), so scoring a held-out item
with the FULL history leaks the answer. To score it fairly you must rebuild the
household's state **as of a cutoff time T** and feed THAT to the scorer.

This module derives the time-dependent inputs from timestamped play events:
  * ``watched_ids``   — items completed at/above threshold before T
  * ``completion``    — per item, the max completion fraction before T
  * ``watch_count``   — per item, the number of plays before T

The static, time-invariant scorer inputs (ratings, credits, collection, genres) come
straight from the movie_files row and need no replay. Affinity (``genre_affinity``)
is recomputed by the service adapter calling the REAL production
``compute_genre_affinity`` on the pre-T entries — not reimplemented here — so the
baseline stays faithful.

PURE: stdlib only (brain-purity safe). Event = mapping with item/time/completion keys.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Hashable, Mapping, Sequence

Item = Hashable


def household_state_at(
    events: Sequence[Mapping],
    cutoff=None,
    *,
    item_key: str = "item",
    time_key: str = "ts",
    completion_key: str = "completion",
    watched_threshold: float = 0.9,
) -> dict:
    """Rebuild (watched_ids, completion, watch_count) from plays strictly BEFORE
    ``cutoff`` (all plays if ``cutoff`` is None).

    ``completion`` per play is a 0-1 fraction; per item we keep the MAX (a movie is
    "watched" if any single play reached the threshold) and count plays for rewatch.
    Returns a dict so callers can pick what they need."""
    max_completion: dict[Item, float] = defaultdict(float)
    plays: dict[Item, int] = defaultdict(int)

    for e in events:
        if cutoff is not None and e[time_key] >= cutoff:
            continue
        item = e[item_key]
        c = float(e.get(completion_key) or 0.0)
        if c > max_completion[item]:
            max_completion[item] = c
        plays[item] += 1

    watched_ids = {it for it, c in max_completion.items() if c >= watched_threshold}
    return {
        "watched_ids": watched_ids,
        "completion": dict(max_completion),
        "watch_count": dict(plays),
    }


def future_watched_items(
    events: Sequence[Mapping],
    cutoff,
    *,
    item_key: str = "item",
    time_key: str = "ts",
    completion_key: str = "completion",
    watched_threshold: float = 0.9,
    exclude: set | None = None,
) -> set:
    """The set of items first *completed* at/after ``cutoff`` — the held-out relevant
    set for a by-time split. Items already in ``exclude`` (e.g. watched before T) are
    dropped so the metric rewards predicting genuinely NEW watches, not re-watches."""
    exclude = exclude or set()
    out: set = set()
    for e in events:
        if e[time_key] < cutoff:
            continue
        if float(e.get(completion_key) or 0.0) >= watched_threshold:
            item = e[item_key]
            if item not in exclude:
                out.add(item)
    return out
