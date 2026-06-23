"""Demand-aware acquisition — the per-user BREADTH signal + the tightness blend.

A downloaded file is SHARED, so the honest value of a grab is how many people will watch it. ``demand`` is
Σ over the household's active users of P(user watches) — a per-user genre match (thresholded, so a near-zero
interest doesn't count); a user with NO history contributes the popularity prior instead of a flat zero.
``demand_priority`` folds that into ``watchability × demand^t`` against the space-tightness ``t`` (see
``machine_learning/space/tightness``): demand is inert when space is roomy (t→0, grab broadly) and dominant
at the floor (t→1, broad appeal wins the scarce budget). PURE — the caller supplies the per-user affinities,
the candidate's popularity, and ``t``.
"""
from __future__ import annotations

from scripts.managers.machine_learning.playlists.per_user import genre_match


def demand_score(genres, user_affinities, *, popularity: float = 0.0,
                 threshold: float = 0.15, gm_opts=None) -> float:
    """Σ over users of P(user watches this candidate). Per user: ``genre_match(genres, their affinity)``
    kept only when it clears ``threshold`` (a near-zero match isn't real demand); a user with NO affinity
    (cold start) contributes the ``popularity`` prior (0–1) instead of a flat zero, so a no-history account
    doesn't drag the breadth signal down. ``user_affinities`` is one ``{genre: weight}`` dict per ACTIVE
    tracked user (one grab serves them all — that shared cost is why breadth is the right currency)."""
    gm_opts = gm_opts or {}
    try:
        pop = max(0.0, min(1.0, float(popularity)))
    except (TypeError, ValueError):
        pop = 0.0
    total = 0.0
    for aff in (user_affinities or []):
        if aff:
            m = genre_match(genres or [], aff, **gm_opts)
            total += m if (isinstance(m, (int, float)) and m >= threshold) else 0.0
        else:
            total += pop                       # cold-start: no taste signal → the popularity prior
    return total


def demand_priority(watchability, demand, t) -> float:
    """The demand-aware acquisition priority: ``watchability × demand^t``. ``t=0`` (roomy) → demand-neutral
    (== watchability, grab broadly); ``t=1`` (at the floor) → ``watchability × demand`` (a 3-user title
    outranks a 1-user title 3:1). ``t`` is clamped to [0,1]. A 0-demand candidate is neutral when roomy but
    falls to 0 as space tightens — grabbed only in genuine abundance."""
    try:
        w = float(watchability)
        d = max(0.0, float(demand))
        tt = max(0.0, min(1.0, float(t)))
    except (TypeError, ValueError):
        return 0.0
    if tt == 0.0:
        return w                               # demand-neutral when there's room to grab broadly
    return w * (d ** tt)                        # d == 0 → 0 once any tightening is in effect
