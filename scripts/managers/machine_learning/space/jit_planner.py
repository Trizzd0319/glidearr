"""space/jit_planner.py — just-in-time per-episode quality-upgrade decision (pure).
==============================================================================
The decision cores of ``sonarr/cache/episode_files.run_jit_quality_upgrades`` (ML Step
7c, the last space planner). JIT bumps the SERIES quality profile for the next unwatched
episode(s) to the best release that still keeps the disk reserve, fires EpisodeSearch, and
a background worker steps down / restores. The method is heavily I/O- and state-interleaved
(per-series profile memoisation, a running ``projected_free``, per-episode id lookups, df
writes, worker spawn), so only the PURE decision slices live here; the service keeps the
fetch + the stateful apply loop.

PURE — pandas + the sizing brain only; no HTTP, no global_cache, no df writes.

Public API:
  * jit_candidates(df, *, max_per_series) -> DataFrame
        next-episode, unwatched, not-yet-upgraded rows, season/episode-sorted, capped per
        series (avoids upgrading a whole short-episode library in one pass).
  * next_up_grab_candidates(df, *, upgrade_cap) -> DataFrame
        unified acquire+re-quality set: missing (no-file) rows in full, on-disk rows capped at
        ``upgrade_cap`` per series — the caller routes both through the same tier/size calibration.
  * jit_reserve_gb(total_gb, upgrade_floor, pct) -> float
        the floor JIT must keep free: max(upgrade_floor U, pct-of-total).
  * jit_row_skip(policy, cert, fid, sid, kids_certs) -> str | None
        per-row guard → 'keep' | 'kids' | 'no_file' | 'no_sid' | None.
  * choose_jit_profile(best_first, *, cap, projected_free, reserve_gb, runtime_min,
                       measured, pressure_cap=None)
        the best profile (highest resolution <= the likelihood cap, further lowered to
        ``pressure_cap`` under space pressure) whose estimated grab still leaves the
        reserve intact; None if even the lowest doesn't fit.
  * jit_step_down_pids(best_first, chosen) -> list
        the chosen profile id + every lower-resolution profile id (the worker's step-down).
        Anchors a per-(series, tier) GROUP ladder when per-episode tiering is on (each tier
        group passes its own group-representative chosen profile).
  * target_tier_key(chosen) -> int
        the chosen profile's max-resolution int — the per-(series, tier) group key used to
        bucket episodes that share a target tier into ONE series-QP-flip search (so the
        background worker never over-grabs a lower-target episode at a higher tier).
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.machine_learning.sizing.size_model import (
    estimate_gb_for_profile,
    profile_max_quality,
)


def jit_candidates(df, *, max_per_series):
    """Next-episode (next_episode=True), unwatched, not-already-upgraded rows — sorted by
    series/season/episode and capped at ``max_per_series`` per series."""
    next_mask = (
        (df["next_episode"] == True) &         # noqa: E712 (pandas truthiness)
        (df["is_watched"] != True) &           # noqa: E712
        (df["upgraded_for_watching"] != True)  # noqa: E712
    )
    all_candidates = df[next_mask].sort_values(
        ["series_id", "season_number", "episode_number"]
    )
    return all_candidates.groupby("series_id", group_keys=False).head(max_per_series)


def next_up_grab_candidates(df, *, upgrade_cap):
    """Unified candidate set for the JIT next-up GRAB pass (acquire + re-quality together).

    Same base as :func:`jit_candidates` (next-episode, unwatched, not-yet-upgraded), but split
    by whether the episode is already on disk and capped PER ACTION:
      * MISSING (no ``episode_file_id``) — kept in FULL. These are fresh acquisitions; the
        prefetch budget already bounds how many are flagged ``next_episode``, so they should
        all be grabbed "just in time" (no extra cap).
      * ON DISK — capped at ``upgrade_cap`` per series, so one run never re-qualifies a whole
        season at once (the historical JIT_MAX_EPISODES bound on re-quality).
    Season/episode-sorted. The caller classifies each row by ``episode_file_id`` (ACQUIRE vs
    UPGRADE/DOWNGRADE) and routes BOTH through the same reserve-aware tier/size calibration."""
    next_mask = (
        (df["next_episode"] == True) &         # noqa: E712
        (df["is_watched"] != True) &           # noqa: E712
        (df["upgraded_for_watching"] != True)  # noqa: E712
    )
    base = df[next_mask].sort_values(["series_id", "season_number", "episode_number"])
    if "episode_file_id" not in base.columns or base.empty:
        return base
    missing = base[base["episode_file_id"].isna()]
    on_disk = (
        base[base["episode_file_id"].notna()]
        .groupby("series_id", group_keys=False).head(upgrade_cap)
    )
    return pd.concat([missing, on_disk]).sort_values(
        ["series_id", "season_number", "episode_number"]
    )


def jit_reserve_gb(total_gb, upgrade_floor, pct) -> float:
    """The free-space floor JIT upgrades must stay above: the larger of the band top
    ``upgrade_floor`` (U) and ``pct`` of the total drive (0 when total is unknown)."""
    pct_reserve = (total_gb * pct) if total_gb and total_gb > 0 else 0.0
    return max(upgrade_floor, pct_reserve)


def jit_row_skip(policy, cert, fid, sid, kids_certs) -> "str | None":
    """Per-candidate guard. Returns the skip bucket or None (eligible):
    'keep' (keep_series/keep_season), 'kids' (kids cert), 'no_file' (not downloaded yet —
    acquisition owns it), 'no_sid' (no series id)."""
    if policy in ("keep_series", "keep_season"):
        return "keep"
    if cert in kids_certs:
        return "kids"
    if pd.isna(fid):
        return "no_file"
    if pd.isna(sid):
        return "no_sid"
    return None


def choose_jit_profile(best_first, *, cap, projected_free, reserve_gb, runtime_min, measured,
                       pressure_cap=None):
    """Best profile for a series' next-up grab: the highest-resolution profile whose max
    resolution is <= the effective cap AND whose estimated grab keeps ``projected_free``
    at/above ``reserve_gb``. ``best_first`` is the profile list ordered highest-resolution
    first. None when even the lowest profile would breach the reserve.

    The effective cap is the likelihood ``cap`` lowered to ``pressure_cap`` when the drive
    is in the lower part of the pressure band (so a near-floor disk grabs 1080p/720p instead
    of 4K even for a hot series). DEFAULT ``pressure_cap=None`` -> the effective cap is the
    bare likelihood ``cap`` and the choice is byte-identical."""
    eff_cap = cap if pressure_cap is None else min(cap, pressure_cap)
    for prof in best_first:
        if profile_max_quality(prof)[0] > eff_cap:
            continue   # exceeds the earned tier (or the pressure ceiling)
        if projected_free - estimate_gb_for_profile(prof, runtime_min, 1, measured) >= reserve_gb:
            return prof
    return None


def jit_step_down_pids(best_first, chosen) -> list:
    """The chosen profile id plus every lower-or-equal-resolution profile id (descending) —
    the worker steps down this ladder until a release grabs. All are <= the chosen profile,
    which already fit the reserve, so they all fit too.

    When per-episode tiering is on, ``chosen`` is the GROUP-representative profile (every
    episode in a (series, tier) group shares the same max resolution), so this ladder is the
    group's divert-down ladder — never reaching above the group's tier, which is what keeps the
    grouped search free of over-grab."""
    chosen_res = profile_max_quality(chosen)[0]
    return [
        p.get("id") for p in best_first
        if profile_max_quality(p)[0] <= chosen_res and p.get("id") is not None
    ]


def target_tier_key(chosen) -> int:
    """The per-(series, tier) group key for a chosen profile: its max-resolution int
    (``profile_max_quality(chosen)[0]``). Episodes whose chosen profiles share this key are
    searched together under ONE series-quality-profile flip — and because the key IS the top
    resolution, their :func:`jit_step_down_pids` ladders are identical, so the group's whole
    step-down stays at or below that tier. Pure (size_model only)."""
    return profile_max_quality(chosen)[0]
