"""
space/downgrade_planner.py — which titles to STEP DOWN, and how far (pure).
================================================================================
Relocated from ``radarr/quality/space_pressure.run_downgrades`` (ML Step 7c). The
DECISION half for BOTH movies and series: pick the lowest-watchability titles and
step their quality profile DOWN the resolution ladder — ONE tier at a time (4K →
1080p → 720p), SPREAD across the eligible pool — until the reclaim target is met. No
title is crushed straight to the floor; the downgrade is shared so many titles drop a
little rather than one dropping a lot. Repeated passes step further while free stays
under the floor.

Stepping is by RESOLUTION TIER (coarse: 4K → 1080p → 720p), not raw profile rank. A
library often has several profiles at one resolution (e.g. WEB-1080p and Remux-1080p);
``step_targets`` collapses each resolution to ONE representative, chosen PER TITLE from
its runtime: the LARGEST profile whose estimated size (rate/min × runtime) is still a real
reduction vs the title's current file — the best-quality downgrade at that resolution, not
the absolute-lowest encode.

PURE — reads the media-files frame cells + the score map + sizing; no HTTP/cache. The
service fetches the ranked profile ladder + the reclaim need (U − free) and APPLIES the
per-title targets (PUT qualityProfileId + search + ledger stamp).

Public API:
  * plan_movie_downgrades(df, score_map, ranked_profiles, *, need_gb, recent_cutoff,
        active_colls, protect_threshold, floor_resolution=720) -> (candidates, stats)
  * plan_series_downgrades(df, ranked_profiles, *, need_gb, ceiling, watch_cutoff,
        air_cutoff, keep_tags, default_runtime_min, floor_resolution=720) -> (candidates, stats)
  * downgrade_reclaim_gb(size_bytes, runtime_minutes, target_profile) -> float
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.machine_learning.sizing.size_model import (
    estimate_gb_for_profile,
    profile_max_quality,
)

DEFAULT_FLOOR_RESOLUTION = 720   # movies/series never step below 720p (universe owns SD)


def _profile_max_res(profile) -> int:
    """Max allowed resolution of a quality profile (0 if none)."""
    res, _ = profile_max_quality(profile) if profile else (-1, None)
    return res if isinstance(res, (int, float)) and res > 0 else 0


def step_targets(ranked_profiles, cur_resolution, cur_gib, est_fn, floor_resolution,
                 *, profile_res=None):
    """Coarse resolution-tier step-down targets for ONE title, sized from its runtime.

    One representative profile per distinct resolution tier strictly below
    ``cur_resolution`` and >= ``floor_resolution``, highest resolution first. The
    representative for a tier is the LARGEST profile whose ESTIMATED size for THIS title
    (``est_fn`` = rate/min × runtime, GiB) is still strictly below the title's current size
    ``cur_gib`` — i.e. the best-quality release at that resolution that is still a real
    reduction, NOT the absolute-lowest encode. Returns ``(targets, cum_reclaim)`` where
    ``cum_reclaim[i] = cur_gib − est(targets[i])`` is the cumulative GiB freed at step i
    (monotonically increasing). Empty when already at/below the floor or nothing reduces.

    ``profile_res`` (optional) is the per-profile max-resolution list parallel to
    ``ranked_profiles``, precomputed ONCE per plan so the same ``profile_max_quality`` walk
    isn't repeated for every title. Default None -> compute it here, byte-identical."""
    try:
        cur = int(cur_resolution)
    except (TypeError, ValueError):
        return [], []
    if cur <= floor_resolution:
        return [], []
    res_list = profile_res if profile_res is not None else [_profile_max_res(p) for p in ranked_profiles]
    by_res: dict[int, tuple] = {}   # resolution -> (est_gib, profile) — keep the LARGEST that still reduces
    for p, r in zip(ranked_profiles, res_list):
        if not (floor_resolution <= r < cur):
            continue
        est = est_fn(p)
        if est < cur_gib and (r not in by_res or est > by_res[r][0]):
            by_res[r] = (est, p)
    targets, cum, last = [], [], cur_gib
    for est, p in sorted(by_res.values(), key=lambda x: -x[0]):   # gentlest (largest) first
        if est < last:                                            # enforce decreasing size
            targets.append(p)
            cum.append(round(cur_gib - est, 6))
            last = est
    return targets, cum


def _spread_to_target(eligible: list, need_gb: float) -> float:
    """Round-robin step-down: each round advances every eligible title one tier deeper
    (input order = priority, lowest score first), accumulating its cumulative reclaim,
    until ``need_gb`` is met or every title has reached the floor. Mutates each item's
    ``_depth`` (number of tiers stepped). Returns the total projected reclaim.

    Each item carries ``cum_reclaim`` — a list whose i-th entry is the cumulative GiB
    freed if the title is stepped to ``targets[i]`` (increasing, since deeper = smaller)."""
    for e in eligible:
        e["_depth"] = 0
    total = 0.0
    progressed = True
    while total < need_gb and progressed:
        progressed = False
        for e in eligible:
            d = e["_depth"]
            cum = e["cum_reclaim"]
            if d >= len(cum):
                continue                            # exhausted tiers (at the floor)
            prev = cum[d - 1] if d > 0 else 0.0
            total += cum[d] - prev
            e["_depth"] = d + 1
            progressed = True
            if total >= need_gb:
                break
    return total


def plan_movie_downgrades(
    df,
    score_map: dict,
    ranked_profiles: list,
    *,
    need_gb: float,
    recent_cutoff,
    active_colls: set,
    protect_threshold: float,
    floor_resolution: int = DEFAULT_FLOOR_RESOLUTION,
) -> "tuple[list[dict], dict[str, int]]":
    """Step the lowest-watchability movies DOWN the resolution ladder — one tier at a
    time, SPREAD across the eligible pool — until ``need_gb`` is reclaimed. No title is
    sent straight to the floor; each settles wherever the accumulating reclaim crosses
    need_gb (or at the floor resolution).

    Guards (skip): keep_forever / keep_movie / keep_universe / bare universe (universe
    quality is owned by the universe manager); recently watched; high watchability score
    (>= protect_threshold); already at/below the floor resolution; first-step reclaim <= 0
    (current file already smaller than the next-lower tier — stepping down would re-grab a
    BIGGER file). Returns ``(candidates, stats)``; candidates (lowest score first) carry
    the chosen ``target_profile`` + cumulative ``reclaim_gb``."""
    stats = {
        "candidates_found": 0, "already_at_720p": 0, "skipped_protected": 0,
        "skipped_high_score": 0, "skipped_recent": 0, "est_reclaim_gb": 0.0,
        "target_met": False,
    }
    if not ranked_profiles:
        return [], stats
    # Profile max-resolutions don't change across titles — resolve once, not per title.
    _profile_res = [_profile_max_res(p) for p in ranked_profiles]

    eligible: list[dict] = []
    for idx in df.index:
        keep_policy = df.at[idx, "keep_policy"] if "keep_policy" in df.columns else None
        is_watched  = bool(df.at[idx, "is_watched"]) if "is_watched" in df.columns else False
        lw          = df.at[idx, "last_watched_at"] if "last_watched_at" in df.columns else None
        coll        = df.at[idx, "collection_name"] if "collection_name" in df.columns else None
        cur_res     = df.at[idx, "resolution"] if "resolution" in df.columns else None
        cur_name    = df.at[idx, "quality_profile_name"] if "quality_profile_name" in df.columns else "?"
        size_bytes  = df.at[idx, "size_bytes"] if "size_bytes" in df.columns else None
        runtime     = df.at[idx, "runtime_minutes"] if "runtime_minutes" in df.columns else None
        movie_id    = df.at[idx, "movie_id"] if "movie_id" in df.columns else None
        score       = score_map.get(idx, 5)

        if pd.isna(movie_id):
            continue
        if keep_policy in ("keep_forever", "keep_movie"):
            stats["skipped_protected"] += 1
            continue
        # Universe quality (keep_universe AND bare universe) is owned by the universe
        # manager. Space-pressure still DELETES bare 'universe' as a last resort.
        if keep_policy in ("keep_universe", "universe"):
            stats["skipped_protected"] += 1
            continue
        if is_watched and lw:
            try:
                if pd.to_datetime(lw, utc=True) >= recent_cutoff:
                    stats["skipped_recent"] += 1
                    continue
            except Exception:
                pass
        if score >= protect_threshold:
            stats["skipped_high_score"] += 1
            continue

        if pd.isna(cur_res):
            stats["already_at_720p"] += 1
            continue
        try:
            cur_gib = float(size_bytes) / (1024 ** 3) if (size_bytes is not None and pd.notna(size_bytes)) else 0.0
        except (TypeError, ValueError):
            cur_gib = 0.0

        def _est(p, _rt=runtime):
            return (estimate_gb_for_profile(p, float(_rt), 1)
                    if (_rt is not None and pd.notna(_rt) and float(_rt) > 0) else 0.0)

        targets, cum = step_targets(ranked_profiles, cur_res, cur_gib, _est, floor_resolution,
                                    profile_res=_profile_res)
        if not targets:
            # At/below the floor, or no lower-resolution profile is a real reduction
            # (stepping down would re-grab a BIGGER file). Nothing to step.
            stats["already_at_720p"] += 1
            continue

        if not is_watched:
            reason = f"never watched (score={score})"
        elif coll and pd.notna(coll) and str(coll) in active_colls:
            reason = f"collection '{coll}' active, score={score}"
        else:
            reason = f"low watchability score ({score})"

        eligible.append({
            "idx": idx, "movie_id": int(movie_id), "targets": targets, "cum_reclaim": cum,
            "score": score, "reason": reason, "cur_name": cur_name,
        })

    eligible.sort(key=lambda e: e["score"])
    total = _spread_to_target(eligible, need_gb)

    candidates: list[dict] = []
    for e in eligible:
        d = e["_depth"]
        if d <= 0:
            continue
        tp = e["targets"][d - 1]
        candidates.append({
            "idx": e["idx"], "movie_id": e["movie_id"],
            "target_profile": tp, "target_id": tp.get("id"),
            "target_name": tp.get("name", str(tp.get("id"))),
            "cur_name": e["cur_name"], "reclaim_gb": round(e["cum_reclaim"][d - 1], 3),
            "reason": e["reason"], "score": e["score"],
        })

    candidates.sort(key=lambda c: c["score"])
    stats["candidates_found"] = len(candidates)
    stats["est_reclaim_gb"]   = round(total, 2)
    stats["target_met"]       = total >= need_gb
    return candidates, stats


def downgrade_reclaim_gb(size_bytes, runtime_minutes, target_profile) -> float:
    """Estimated GiB freed by a downgrade: current file size minus the estimated size
    at the target profile's top quality (>= 0). 0 when runtime is unknown."""
    sz_f = float(size_bytes) if (size_bytes is not None and pd.notna(size_bytes)) else 0.0
    est_target = (
        estimate_gb_for_profile(target_profile, float(runtime_minutes), 1)
        if (runtime_minutes is not None and pd.notna(runtime_minutes) and float(runtime_minutes) > 0)
        else 0.0
    )
    return max(0.0, (sz_f / (1024 ** 3)) - est_target)


def _max_ts(series):
    """Latest non-null UTC timestamp in a column, or None (pure helper)."""
    try:
        s = pd.to_datetime(series, utc=True, errors="coerce").dropna()
        return s.max() if len(s) else None
    except Exception:
        return None


def plan_series_downgrades(
    df,
    ranked_profiles: list,
    *,
    need_gb: float,
    ceiling: float,
    watch_cutoff,
    air_cutoff,
    keep_tags,
    default_runtime_min: float,
    floor_resolution: int = DEFAULT_FLOOR_RESOLUTION,
) -> "tuple[list[dict], dict[str, int]]":
    """Series twin of plan_movie_downgrades: aggregate episode rows per series (max
    watchability score, keep_policy, on-disk episode count, total bytes, max resolution),
    apply the guards, then STEP each eligible series DOWN the resolution ladder one tier at
    a time, spread across the pool, until ``need_gb`` is reclaimed.

    Guards (skip): keep tag; high score (>= ceiling); nothing on disk; already at/below the
    floor resolution; recently WATCHED; recently AIRED; first-step reclaim <= 0.

    Returns ``(candidates, stats)``; each candidate carries sid/title/score/n_eps/cur_gib/
    indices and the chosen ``target_profile`` (+ id/name) + cumulative ``reclaim_gb``. PURE.
    The service applies each per-series target (PUT + SeriesSearch + stamp)."""
    stats = {
        "candidates": 0, "skipped_protected": 0, "skipped_high_score": 0,
        "skipped_recent": 0, "skipped_already": 0, "est_reclaim_gb": 0.0,
        "target_met": False,
    }
    if not ranked_profiles:
        return [], stats
    # Profile max-resolutions don't change across series — resolve once, not per series.
    _profile_res = [_profile_max_res(p) for p in ranked_profiles]

    eligible: list[dict] = []
    for series_id, rows in df.groupby("series_id", sort=False):
        try:
            sid = int(series_id)
        except (TypeError, ValueError):
            continue

        score_vals = pd.to_numeric(rows["watchability_score"], errors="coerce").dropna()
        if not len(score_vals):
            continue
        score = float(score_vals.max())   # constant per series; max ignores NaN

        keep_policy = None
        if "keep_policy" in rows.columns:
            kp = rows["keep_policy"].dropna()
            keep_policy = str(kp.iloc[0]) if len(kp) else None

        file_rows = rows[pd.to_numeric(rows.get("size_bytes"), errors="coerce").fillna(0) > 0] \
            if "size_bytes" in rows.columns else rows.iloc[0:0]
        n_eps = int(len(file_rows))

        title = str(rows["series_title"].dropna().iloc[0]) if "series_title" in rows.columns \
            and len(rows["series_title"].dropna()) else f"series {sid}"

        # ── protections ──
        if keep_policy in keep_tags:
            stats["skipped_protected"] += 1
            continue
        if score >= ceiling:
            stats["skipped_high_score"] += 1
            continue
        if n_eps == 0:
            continue   # nothing on disk to reclaim
        max_res = pd.to_numeric(file_rows.get("resolution"), errors="coerce").max() \
            if "resolution" in file_rows.columns else None
        if max_res is None or pd.isna(max_res) or int(max_res) <= floor_resolution:
            stats["skipped_already"] += 1
            continue   # already at/below the floor — nothing to step down
        last_watched = _max_ts(file_rows.get("last_watched_at")) if "last_watched_at" in file_rows.columns else None
        if last_watched is not None and last_watched >= watch_cutoff:
            stats["skipped_recent"] += 1
            continue
        last_air = _max_ts(file_rows.get("air_date_utc")) if "air_date_utc" in file_rows.columns else None
        if last_air is not None and last_air >= air_cutoff:
            stats["skipped_recent"] += 1
            continue

        # ── reclaim context (whole-series re-grab at a target profile) ──
        total_bytes = float(pd.to_numeric(file_rows["size_bytes"], errors="coerce").fillna(0).sum())
        cur_gib = total_bytes / (1024 ** 3)
        rt = pd.to_numeric(file_rows.get("runtime_seconds"), errors="coerce").dropna() \
            if "runtime_seconds" in file_rows.columns else pd.Series([], dtype="float64")
        avg_rt_min = (float(rt.mean()) / 60.0) if len(rt) and rt.mean() > 0 else default_runtime_min

        def _est(p, _rt=avg_rt_min, _n=n_eps):
            return estimate_gb_for_profile(p, _rt, _n) or 0.0

        targets, cum = step_targets(ranked_profiles, int(max_res), cur_gib, _est, floor_resolution,
                                    profile_res=_profile_res)
        if not targets:
            stats["skipped_already"] += 1
            continue

        eligible.append({
            "sid": sid, "title": title, "score": score, "n_eps": n_eps, "cur_gib": cur_gib,
            "indices": list(rows.index), "targets": targets, "cum_reclaim": cum,
            "reason": (f"score {score:.0f} < {ceiling:.0f}" if keep_policy is None
                       else f"score {score:.0f} < {ceiling:.0f} ({keep_policy})"),
        })

    eligible.sort(key=lambda e: e["score"])
    total = _spread_to_target(eligible, need_gb)

    candidates: list[dict] = []
    for e in eligible:
        d = e["_depth"]
        if d <= 0:
            continue
        tp = e["targets"][d - 1]
        candidates.append({
            "sid": e["sid"], "title": e["title"], "score": e["score"],
            "n_eps": e["n_eps"], "cur_gib": e["cur_gib"], "indices": e["indices"],
            "target_profile": tp, "target_id": tp.get("id"),
            "target_name": tp.get("name", str(tp.get("id"))),
            "reclaim": round(e["cum_reclaim"][d - 1], 3),
            "reason": e["reason"],
        })

    candidates.sort(key=lambda c: c["score"])
    stats["candidates"]     = len(candidates)
    stats["est_reclaim_gb"] = round(total, 2)
    stats["target_met"]     = total >= need_gb
    return candidates, stats
