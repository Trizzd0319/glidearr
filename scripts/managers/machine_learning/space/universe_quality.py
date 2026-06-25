"""space/universe_quality.py — universe-tag quality-tier DECISION core (pure).
==============================================================================
The decision half of ``radarr/quality/universe`` (ML migration Step 7c). PURE — pandas
+ the likelihood / sizing / downgrade-planner brain modules only; NO HTTP, NO service
imports, NO global_cache. The service keeps FETCH (ranked profiles, movie payload),
the Parquet load/save, and the GET/PUT APPLY half; it delegates the decisions here.

Universe quality follows the SAME band as the rest of space management (space_targets):
downgrade below the floor T, upgrade above the band top U, hold in [T, U].

Public API:
  * universe_action(free_space_gb, downgrade_threshold, upgrade_threshold) -> str | None
        — "downgrade" / "upgrade" / None (hold).
  * upgrade_target(ranked_profiles, current_profile_id, likelihood, config, *, min_rank=0)
        — likelihood-gated ladder step-UP (Radarr profile-id ladder); None if at/above tier.
  * downgrade_target(row, ranked_profiles, current_profile_id, config, *, min_rank=0)
        — one resolution-tier step DOWN (best-quality, runtime-sized via step_targets);
          legacy single-rank fallback when the row's resolution is unknown.
  * downgrade_single_rank(ranked_profiles, current_profile_id, *, min_rank=0)
        — one ranked-list rank down (the legacy/fallback step).
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.machine_learning.likelihood.watch_likelihood import (
    english_ladder_ids,
    ladder_rank,
    profile_id_for_likelihood,
    radarr_ladder,
    radarr_ladder_english,
    resolution_cap_for_likelihood,
)
from scripts.managers.machine_learning.sizing.size_model import estimate_gb_for_profile
from scripts.managers.machine_learning.space.downgrade_planner import (
    UNIVERSE_PROTECT_MIN,
    step_targets,
)
from scripts.managers.machine_learning.space.dual_version import hd_capped_likelihood


def universe_action(free_space_gb, downgrade_threshold, upgrade_threshold) -> "str | None":
    """The band decision: free < T → 'downgrade' (reclaim under pressure); free > U →
    'upgrade'; in the hold band [T, U] → None. Strict inequalities = the boundaries hold
    (free == T or == U → no action), matching the hysteresis of the other space gates."""
    if free_space_gb < downgrade_threshold:
        return "downgrade"
    if free_space_gb > upgrade_threshold:
        return "upgrade"
    return None


def upgrade_target(ranked_profiles, current_profile_id, likelihood, config, *, min_rank=0):
    """Likelihood-gated UPGRADE target via the explicit Radarr profile-id ladder
    (distinguishes low/high-1080p and low/high-4K which share a resolution). Only ever
    upgrades (target rank > current rank); if the earned profile isn't configured in
    Radarr, steps DOWN the ladder to the highest present profile still above current.
    None when already at/above the earned tier."""
    if not ranked_profiles:
        return None
    L = likelihood if likelihood is not None else 0.0
    # English-locked films (current profile is an English twin tier) climb the PARALLEL
    # English ladder, so an upgrade never drops them onto a mixed-language tier. Without
    # this, ladder_rank(english_id) == -1 on the normal ladder would force a spurious
    # upgrade onto a normal profile and silently strip the dub. Off (no english ladder
    # configured / current not English) → identical to before.
    english     = int(current_profile_id) in english_ladder_ids(config)
    # proactive_4k single-authority cap: when actuating, never bump the standard instance to 4K
    # here — the 4K copy is acquired on the dedicated 4K instance by the reconcile. No-op otherwise.
    L           = hd_capped_likelihood(L, ranked_profiles, config, english=english)
    target_id   = profile_id_for_likelihood(L, config=config, english=english)
    target_rank = ladder_rank(target_id, config=config, english=english)
    cur_rank    = ladder_rank(current_profile_id, config=config, english=english)
    if target_rank <= cur_rank:
        return None
    by_id = {p.get("id"): p for p in ranked_profiles}
    lad = radarr_ladder_english(config) if english else radarr_ladder(config)
    for r in range(target_rank, cur_rank, -1):
        pid = int(lad[r][1])
        if pid in by_id:
            return by_id[pid]
    return None


def downgrade_single_rank(ranked_profiles, current_profile_id, *, min_rank=0):
    """One ranked-LIST rank down (the legacy step / unknown-resolution fallback). None at
    the floor (``current rank <= min_rank``)."""
    if not ranked_profiles:
        return None
    ids = [p["id"] for p in ranked_profiles]
    try:
        ci = ids.index(current_profile_id)
    except ValueError:
        ci = 0
    return None if ci <= min_rank else ranked_profiles[ci - 1]


def downgrade_target(row, ranked_profiles, current_profile_id, config, *, min_rank=0, likelihood=None):
    """One RESOLUTION-TIER step down — same runtime-sized logic movies/TV use
    (downgrade_planner.step_targets): the best-quality profile at the next-lower resolution
    whose estimated grab (rate/min × runtime) still reduces the current file. Universe
    floors at the LOWEST real resolution (``floor_resolution=1``) so franchises can bottom
    out below the 720p movie/TV floor over passes. None when nothing reduces. Falls back to
    ``downgrade_single_rank`` when the row's resolution is unknown.

    Borrowed franchise/universe credit: a HOT member (its ``universe_credit`` row field >=
    UNIVERSE_PROTECT_MIN, with ``likelihood`` supplied) floors at the resolution its credit-bearing
    likelihood EARNS (``resolution_cap_for_likelihood``) instead of resolution 1 — so a rewatched saga
    resists the step-down to its earned tier (and HOLDS, returning None, once it is at/under that tier).
    As the credit DECAYS below the threshold the member reverts to the universe floor (1) and is fully
    droppable again — the user's "recency bias drop." Cold / no-credit members are byte-identical."""
    cur_res = row.get("resolution") if hasattr(row, "get") else None
    if cur_res is not None and pd.notna(cur_res):
        sz = row.get("size_bytes")
        rt = row.get("runtime_minutes")
        cur_gib = float(sz) / (1024 ** 3) if (sz is not None and pd.notna(sz)) else 0.0

        def _est(p, _rt=rt):
            return (estimate_gb_for_profile(p, float(_rt), 1)
                    if (_rt is not None and pd.notna(_rt) and float(_rt) > 0) else 0.0)

        floor_res = 1
        if likelihood is not None:
            uc = row.get("universe_credit") if hasattr(row, "get") else None
            try:
                uc = float(uc) if (uc is not None and pd.notna(uc)) else 0.0
            except (TypeError, ValueError):
                uc = 0.0
            if uc >= UNIVERSE_PROTECT_MIN:
                floor_res = resolution_cap_for_likelihood(likelihood, config=config)

        targets, _cum = step_targets(ranked_profiles, cur_res, cur_gib, _est, floor_res)
        return targets[0] if targets else None
    return downgrade_single_rank(ranked_profiles, current_profile_id, min_rank=min_rank)
