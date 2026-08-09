"""
plex/playlists/combined_resolver.py — one cross-medium "Up Next" from TV + movie candidates.
================================================================================
Merges the candidate ``PlaylistInput``s from ``tv_inputs`` (episodes) and ``movie_inputs``
(movies) and orders them on ONE comparable axis. Because a TV watchability score and a movie
score come from different scorers/scales, ordering uses ``normalize_per_medium=True`` — each
item is ranked by its percentile WITHIN its medium, so neither medium dominates and a
top-percentile show interleaves fairly with a top-percentile film. Franchise/universe
grouping can even span mediums (a Star Wars film + the Clone Wars series under one "Star
Wars" universe). Pure given its inputs.
"""
from __future__ import annotations

from scripts.managers.machine_learning.playlists.ordering import order_items


def build_combined_plan(input_lists, *, family: str = "up_next", max_items: int = 100,
                        resume_boost: bool = False, resume_order: str = "recency",
                        resume_weight: float = 0.0,
                        recency_boost: bool = False, window_days: int = 30,
                        recency_mode: str = "tier", recency_weight: float = 0.35,
                        warmth_weight: float = 0.0, warmth_halflife_hours: float = 18.0,
                        now_ts=None, saga_boost=None,
                        progress_filter: str | None = None, series_recency=None,
                        max_per_group: int | None = None):
    """Order the merged candidates from one or more mediums into a single cross-medium plan.
    ``input_lists`` is an iterable of ``PlaylistInput`` lists (e.g. ``[tv_items, movie_items]``).
    Returns ``(PlaylistPlan, stats)``. ``resume_boost`` lifts an in-progress saga; ``recency_boost``
    lifts a caught-up saga/series whose freshest member landed within ``window_days`` (precedence
    over resume_boost). ``progress_filter`` ("in"/"out") slices the pool into the mood lists (The
    Long Glide / Touch & Go); ``series_recency`` (``{series_id: (last_ts, count)}``) carries
    in-progress shows' recency + depth (their watched episodes are pre-filtered), so TV resumes
    alongside movie sagas.

    ``recency_mode`` / ``warmth_weight`` (GLD-PLY-17) — with ``recency_mode="tier"`` (the legacy
    default) ``recency_boost`` SHORT-CIRCUITS the whole resume/saga/warmth path in
    :func:`order_items`, so those signals are inert whenever it is on. ``"blend"`` makes them
    compose. ``warmth_weight`` is the "you were watching this a few hours ago" lift; it decays by
    half every ``warmth_halflife_hours``. ``saga_boost`` was previously not forwarded from here at
    all, so the completion signal never reached the combined Up Next."""
    merged = [it for lst in input_lists for it in (lst or [])]
    plan = order_items(merged, family=family, max_items=max_items, normalize_per_medium=True,
                       resume_boost=resume_boost, resume_order=resume_order,
                       resume_weight=resume_weight,
                       recency_boost=recency_boost, window_days=window_days,
                       recency_mode=recency_mode, recency_weight=recency_weight,
                       warmth_weight=warmth_weight,
                       warmth_halflife_hours=warmth_halflife_hours, now_ts=now_ts,
                       saga_boost=saga_boost,
                       progress_filter=progress_filter, series_recency=series_recency,
                       max_per_group=max_per_group)
    by_medium: dict = {}
    for it in merged:
        by_medium[it.medium] = by_medium.get(it.medium, 0) + 1
    return plan, {"considered": len(merged), "in_plan": len(plan.items), "by_medium": by_medium}
