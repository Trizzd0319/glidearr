"""
space/coordinator_ranker.py — unified delete-pool ranking (pure).
================================================================================
Relocated from ``coordinator/space_coordinator._select_for_target`` /
``_critic_sort`` (ML Step 7a — first cut of the space planners). The ONE ranker
that orders the merged movie+episode delete pool and greedily accumulates to the
free-space target. PURE — operates on plain candidate dicts ({score, critic,
size_gb, ...}, the DeleteCandidate shape); no HTTP, no global_cache. The
coordinator keeps the stage orchestration + free-space FETCH + the service
delete_selected_* APPLY, and delegates the ranking here.

Public API:
  * critic_sort(critic) -> float
        sort key for critic rating; None (episodes carry none) -> neutral 5.0.
  * recency_bonus(candidate, ramp, now) -> float
        score bump from how recently a candidate was last watched (0.0 when off).
  * select_for_target(pool, need_gb, *, recency_ramp, now, tier_size) -> (selected, projected_gb)
        rank lowest-watchability-first (then lowest critic, then biggest file) and
        accumulate from the bottom until projected reclaim reaches need_gb. tier_size
        coarsens the score into buckets so the biggest file in the lowest tier goes
        first (fewer deletions per target).
"""
from __future__ import annotations

import math

import pandas as pd


def critic_sort(critic) -> float:
    """Sort key for critic rating — None (episodes carry no critic) sorts to a
    neutral mid value so a missing critic neither protects nor condemns."""
    try:
        return float(critic) if critic is not None else 5.0
    except (TypeError, ValueError):
        return 5.0


def recency_bonus(candidate: dict, ramp: dict, now) -> float:
    """Score bump from how recently ``candidate`` was last watched — a file watched
    minutes ago sorts *later* in the delete pool (deleted last), protecting the active
    title from the bottom-first sweep. ``weight * exp(-age_days / half_life_days)`` from
    the candidate's ``last_watched_at`` and ``now`` (a tz-aware Timestamp). 0.0 when the
    anchor is missing or unparseable, so a never-watched candidate keeps its base score
    and the ranking is unchanged for it."""
    anchor = candidate.get("last_watched_at")
    if not anchor:
        return 0.0
    try:
        watched = pd.to_datetime(anchor, utc=True)
        age_days = (now - watched).total_seconds() / 86400.0
    except Exception:
        return 0.0
    if age_days < 0:
        age_days = 0.0
    half_life = float(ramp.get("half_life_days", 30) or 30)
    weight = float(ramp.get("weight", 0) or 0)
    return weight * math.exp(-age_days / half_life)


def select_for_target(pool: list[dict], need_gb: float, *,
                      recency_ramp: "dict | None" = None, now=None,
                      tier_size: "float | None" = None,
                      uhd_first: bool = False) -> "tuple[list[dict], float]":
    """Rank the combined movie+episode pool (lowest watchability first, then lowest
    critic, then biggest file first) and greedily accumulate from the bottom until
    projected reclaim reaches ``need_gb``. Returns ``(selected, projected_gb)``.
    Pure — unit-testable without the manager graph.

    Three optional, independently default-off refinements; with ALL off the sort key is
    the exact ``(score, critic, -size)`` it has always been (byte-identical):

      * ``recency_ramp`` enabled AND ``now`` supplied -> the score term gains a recency
        bonus (``recency_bonus``) so a recently-watched file sinks to the bottom of the
        delete order. The brain takes no clock — ``now`` comes from the service.
      * ``tier_size`` truthy -> the score term is coarsened into buckets of that width
        (``floor(score / tier_size)``) so within a watchability tier the BIGGEST file
        goes first — fewer deletions to reclaim ``need_gb`` and a stabler order under
        small score jitter.
      * ``uhd_first`` -> a candidate flagged ``is_uhd_copy`` (a dual-version 4K BONUS copy
        whose 1080p baseline survives on the standard instance, so deleting it loses NO
        title — pure reclaim) sorts AHEAD of every whole-title candidate. Within the 4K
        group the existing ``(score, critic, -size)`` order still applies, so the
        least-watchable, biggest 4K copies go first and the greedy accumulator stops as
        soon as the target is met — whole titles are touched only if no 4K reclaim is left."""
    use_recency = bool(recency_ramp and recency_ramp.get("enabled") and now is not None)
    _now = pd.to_datetime(now, utc=True) if use_recency else None

    def _score_term(c):
        s = c.get("score", 5)
        if use_recency:
            s = s + recency_bonus(c, recency_ramp, _now)
        return s

    def _uhd_rank(c):
        # 0 = a baseline-backed 4K bonus copy → evict first (pure reclaim); 1 = everything else.
        return 0 if (uhd_first and c.get("is_uhd_copy")) else 1

    if tier_size:
        _ts = float(tier_size)
        key = lambda c: (_uhd_rank(c), math.floor(_score_term(c) / _ts),
                         critic_sort(c.get("critic")), -float(c.get("size_gb") or 0.0))
    else:
        key = lambda c: (_uhd_rank(c), _score_term(c),
                         critic_sort(c.get("critic")), -float(c.get("size_gb") or 0.0))
    ordered = sorted(pool, key=key)
    selected: list[dict] = []
    projected = 0.0
    for c in ordered:
        if projected >= need_gb:
            break
        selected.append(c)
        projected += float(c.get("size_gb") or 0.0)
    return selected, projected
