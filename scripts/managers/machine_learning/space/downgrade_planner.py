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

EXHAUSTIVE MODE (``space_exhaustive_downgrade``, DEFAULT ON at the service boundary) —
"deletion is the true last resort": downgrade EVERYTHING that can still be downgraded
before ANYTHING is deleted. Both planners then (a) stop applying the watchability score
CEILING as an eligibility filter and (b) run the spread to FULL DEPTH instead of stopping
at ``need_gb`` — so every title above the 720p floor is planned down to it, still ordered
ASCENDING by watchability (least-valued shrinks first). Every other guard is unchanged.
The 720p floor is what makes the invariant work: the delete pools only accept items already
AT or BELOW it, so a title with anything left to shrink can never be deleted. The pass that
APPLIES the plan still stops as soon as free space (net of in-flight re-grabs) reaches the
band top U, and is bounded by ``space_downgrade_max_regrabs_per_run``.

Public API:
  * plan_movie_downgrades(df, score_map, ranked_profiles, *, need_gb, recent_cutoff,
        active_colls, protect_threshold, floor_resolution=720, exhaustive=False)
        -> (candidates, stats)
  * plan_series_downgrades(df, ranked_profiles, *, need_gb, ceiling, watch_cutoff,
        air_cutoff, keep_tags, default_runtime_min, floor_resolution=720, exhaustive=False)
        -> (candidates, stats)
  * downgrade_reclaim_gb(size_bytes, runtime_minutes, target_profile) -> float
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.machine_learning.sizing.size_model import (
    estimate_gb_for_profile,
    profile_max_quality,
)

DEFAULT_FLOOR_RESOLUTION = 720   # movies/series never step below 720p (universe owns SD)


# ── UNSCORED ROWS ARE DEFERRED, NOT SCORED WITH A GUESS ───────────────────────
# Both space planners used to read ``score_map.get(idx, 5)`` (and the two service-side
# pool builders ``... if pd.notna(sc) else 5``): a row the scorer produced no value for
# was handed the literal 5 and carried straight on into the queue.
#
# WHAT WAS WRONG WITH IT. 5 is a POINT ON THE SCORE AXIS, so it silently changed meaning
# every time that axis moved. When it was chosen it sat at p1.3 of the movie distribution
# — "an unscored row is the least valuable thing in the library, delete it first". After
# Group D v2 turned a near-constant +12 bonus into a 0-to-negative transcode-risk penalty,
# the SAME literal 5 sits at p32.4 — "delete it mid-pack". Nobody decided either of those;
# both fell out of a constant that did not move with the distribution beneath it.
#
# WHAT IT IS FOR, honestly answered: nothing that survives being written down. The policy
# it was standing in for is already stated one level up, and stated correctly — both delete
# pool builders REFUSE to contribute any candidate when the whole ``watchability_score``
# column is empty ("won't delete on fallback scores"). The row-level sentinel contradicted
# that policy for the partial case. So the sentinel is gone and the two agree: an unscored
# row DEFERS. That is the answer every other missing-data branch in this codebase already
# gives — ``anomaly._score_owned`` defers on uncached credits, ``series_monitor_action``
# defers when ``has_score`` is false, ``build_delete_candidates`` fails safe when the
# protected-id build throws. Waiting costs the row nothing: refresh_scores fills it in on
# the next pass, and a deferred row is inert (not deleted AND not downgraded), so it can
# neither be shed on invented data nor block anything else from being shed.
#
# THE FAILURE MODE THIS OPENS, and how it is closed: a row that is PERSISTENTLY unscored
# would become quietly immortal. So it is COUNTED, never silently dropped —
# ``stats['skipped_unscored']`` on both planners, ``last_skipped_unscored`` + a log line on
# both service pool builders. A silent mis-ranking becomes a visible number.
_MISSING = object()


def row_score(score_map, idx):
    """The row's score, or ``None`` when the scorer produced none (see the note above).

    ``None``/NaN entries count as absent: a score map built from a Parquet column can
    contain NaN for a row the last ``refresh_scores`` never reached, and that is the same
    "no value" as a key that isn't there at all."""
    v = score_map.get(idx, _MISSING)
    if v is _MISSING or v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


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


def _spread_to_target(eligible: list, need_gb: float, *, exhaustive: bool = False) -> float:
    """Round-robin step-down: each round advances every eligible title one tier deeper
    (input order = priority, lowest score first), accumulating its cumulative reclaim,
    until ``need_gb`` is met or every title has reached the floor. Mutates each item's
    ``_depth`` (number of tiers stepped). Returns the total projected reclaim.

    Each item carries ``cum_reclaim`` — a list whose i-th entry is the cumulative GiB
    freed if the title is stepped to ``targets[i]`` (increasing, since deeper = smaller).

    ``exhaustive`` (``space_exhaustive_downgrade``, DEFAULT-ON at the service boundary):
    ignore ``need_gb`` entirely and keep spreading until EVERY eligible title has reached
    the floor — the plan then covers everything that can still be downgraded, which is the
    precondition for the "deletion is the true last resort" invariant (a title is only
    delete-eligible once it is at/below the floor). The pass that APPLIES the plan is what
    stops at the band top U (against a free-space figure net of in-flight re-grabs), so
    "exhaustive" means "no early stop at a partial projected target", not "downgrade the
    whole library regardless of need". Default False → byte-identical to the historical
    spread."""
    for e in eligible:
        e["_depth"] = 0
    total = 0.0
    progressed = True
    while progressed and (exhaustive or total < need_gb):
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
            if not exhaustive and total >= need_gb:
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
    exhaustive: bool = False,
) -> "tuple[list[dict], dict[str, int]]":
    """Step the lowest-watchability movies DOWN the resolution ladder — one tier at a
    time, SPREAD across the eligible pool — until ``need_gb`` is reclaimed. No title is
    sent straight to the floor; each settles wherever the accumulating reclaim crosses
    need_gb (or at the floor resolution).

    ``exhaustive`` (``space_exhaustive_downgrade``, DEFAULT-ON at the service boundary):
    the score CEILING stops being an eligibility filter (titles at/above
    ``protect_threshold`` are ADMITTED and counted in ``over_ceiling_included``) and the
    spread runs to full depth, so every movie above the floor is planned down to it. The
    ordering is unchanged — ascending watchability, least-valued first — and every other
    guard below is untouched. The APPLYING pass is what stops at the band top U.

    Guards (skip): keep_forever / keep_movie / keep_universe / bare universe (universe
    quality is owned by the universe manager — incl. its credit-gated step-down); hot
    franchise/universe credit (>= UNIVERSE_PROTECT_MIN) for movies NOT universe-tagged (the
    common case — saga membership comes from the TMDB collection, no keep tag needed); recently
    watched; high watchability score
    (>= protect_threshold); already at/below the floor resolution; first-step reclaim <= 0
    (current file already smaller than the next-lower tier — stepping down would re-grab a
    BIGGER file). Returns ``(candidates, stats)``; candidates (lowest score first) carry
    the chosen ``target_profile`` + cumulative ``reclaim_gb``."""
    stats = {
        "candidates_found": 0, "already_at_720p": 0, "skipped_protected": 0,
        "skipped_high_score": 0, "skipped_recent": 0, "skipped_universe": 0,
        "est_reclaim_gb": 0.0, "target_met": False,
        # exhaustive-mode observability (0 / False on the legacy path)
        "exhaustive": bool(exhaustive), "over_ceiling_included": 0,
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
        uni_credit  = df.at[idx, "universe_credit"] if "universe_credit" in df.columns else None
        # UNSCORED -> DEFER. See the block comment on delete_planner._row_score: the old
        # ``score_map.get(idx, 5)`` handed an unscored row a literal point on the score
        # axis, which quietly changed meaning (p1.3 -> p32.4) when Group D v2 translated
        # that axis. The step-down ladder and the delete pool are two stages of ONE
        # decision, so they take the same answer to missing data — wait for a real score
        # rather than shrink a file on a guessed one. Counted, never silently dropped.
        score       = row_score(score_map, idx)
        if score is None:
            stats["skipped_unscored"] = stats.get("skipped_unscored", 0) + 1
            continue

        if pd.isna(movie_id):
            continue
        if keep_policy in ("keep_forever", "keep_movie"):
            stats["skipped_protected"] += 1
            continue
        # Universe quality (keep_universe AND bare universe) is owned by the universe manager
        # — including the borrowed-credit step-down floor (universe_quality.downgrade_target).
        # Space-pressure still DELETES bare 'universe' as a last resort.
        if keep_policy in ("keep_universe", "universe"):
            stats["skipped_protected"] += 1
            continue
        # Borrowed franchise/universe credit (per-movie, recency-decayed by refresh_scores): an
        # UNTAGGED saga member (no keep tag — the common case; membership from the TMDB collection)
        # sitting in a HOT saga resists step-down. As the saga goes stale the credit decays below the
        # floor and it becomes droppable again. Keep-tagged universe titles never reach here (skipped
        # above) — their credit-gated step-down is the universe manager's.
        try:
            _uc = float(uni_credit) if (uni_credit is not None and pd.notna(uni_credit)) else 0.0
        except (TypeError, ValueError):
            _uc = 0.0
        if _uc >= UNIVERSE_PROTECT_MIN:
            stats["skipped_universe"] += 1
            continue
        if is_watched and lw:
            try:
                if pd.to_datetime(lw, utc=True) >= recent_cutoff:
                    stats["skipped_recent"] += 1
                    continue
            except Exception:
                pass
        if score >= protect_threshold:
            # EXHAUSTIVE: the watchability ceiling is no longer an eligibility filter —
            # a high-score title still has to shrink before ANY title is deleted. It
            # simply sorts LAST (ascending score), so it is the last to be touched.
            if not exhaustive:
                stats["skipped_high_score"] += 1
                continue
            stats["over_ceiling_included"] += 1

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
    total = _spread_to_target(eligible, need_gb, exhaustive=exhaustive)

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


# A series whose borrowed franchise/universe credit (set by refresh_scores, recency-decayed) is at
# least this many watch-counts is PROTECTED from space-pressure downgrade — a hot saga keeps its
# members at their earned tier. As the saga goes stale the credit decays below this and its members
# become droppable again (the user's "recency bias drop for space being easier to drop them down").
UNIVERSE_PROTECT_MIN = 1.0


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
    exhaustive: bool = False,
) -> "tuple[list[dict], dict[str, int]]":
    """Series twin of plan_movie_downgrades: aggregate episode rows per series (max
    watchability score, keep_policy, on-disk episode count, total bytes, max resolution),
    apply the guards, then STEP each eligible series DOWN the resolution ladder one tier at
    a time, spread across the pool, until ``need_gb`` is reclaimed.

    Guards (skip): keep tag; hot franchise/universe credit (>= UNIVERSE_PROTECT_MIN); high score
    (>= ceiling); nothing on disk; already at/below the floor resolution; recently WATCHED; recently
    AIRED; first-step reclaim <= 0.

    ``exhaustive`` (``space_exhaustive_downgrade``, DEFAULT-ON at the service boundary): the
    ``ceiling`` stops being an eligibility filter (over-ceiling series are ADMITTED and counted in
    ``over_ceiling_included``) and the spread runs to full depth, so every series above the floor is
    planned down to it. Ordering (ascending score) and every other guard are unchanged; the APPLYING
    pass is what stops at the band top U and enforces the re-grab cap.

    Returns ``(candidates, stats)``; each candidate carries sid/title/score/n_eps/cur_gib/
    indices and the chosen ``target_profile`` (+ id/name) + cumulative ``reclaim_gb``. PURE.
    The service applies each per-series target (PUT + SeriesSearch + stamp)."""
    stats = {
        "candidates": 0, "skipped_protected": 0, "skipped_high_score": 0,
        "skipped_recent": 0, "skipped_already": 0, "skipped_universe": 0,
        "est_reclaim_gb": 0.0, "target_met": False,
        # exhaustive-mode observability (0 / False on the legacy path)
        "exhaustive": bool(exhaustive), "over_ceiling_included": 0,
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

        # The series path ALWAYS deferred unscored rows — it is the precedent the movie
        # path has now been brought into line with (see ``row_score``). Only the counter
        # is new, so a persistently-unscored series is visible rather than silently inert.
        score_vals = pd.to_numeric(rows["watchability_score"], errors="coerce").dropna()
        if not len(score_vals):
            stats["skipped_unscored"] = stats.get("skipped_unscored", 0) + 1
            continue
        score = float(score_vals.max())   # constant per series; max ignores NaN

        # Borrowed franchise/universe credit (per-series, recency-decayed by refresh_scores). A hot
        # saga's members carry a high credit and resist downgrade; a stale saga's has decayed away.
        uni_credit = 0.0
        if "universe_credit" in rows.columns:
            _uc = pd.to_numeric(rows["universe_credit"], errors="coerce").dropna()
            uni_credit = float(_uc.max()) if len(_uc) else 0.0

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
        if uni_credit >= UNIVERSE_PROTECT_MIN:
            stats["skipped_universe"] += 1
            continue
        if score >= ceiling:
            # EXHAUSTIVE: the ceiling is no longer an eligibility filter — a high-score
            # series must still shrink before ANY title is deleted; it just sorts LAST.
            if not exhaustive:
                stats["skipped_high_score"] += 1
                continue
            stats["over_ceiling_included"] += 1
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
    total = _spread_to_target(eligible, need_gb, exhaustive=exhaustive)

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
