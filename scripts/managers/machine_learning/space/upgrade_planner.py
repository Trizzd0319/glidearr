"""
space/upgrade_planner.py — likelihood-gated active-watcher upgrades (pure).
================================================================================
Relocated from ``radarr/quality/space_pressure.run_active_watcher_upgrades`` (movies)
and ``sonarr/series/quality.run_active_watcher_upgrades`` (series), ML Step 7c. The
DECISION half: for each title the household is actively watching, decide whether to
upgrade and to what target. PURE — reads the media-files frame + fetched profiles /
series records + config; no HTTP, no global_cache. The service FETCHES (profiles, and
for series the per-series record + tags) and APPLIES (PUT qualityProfileId + search).
The space gate (free >= U) stays in the service.

MOVIES are fully pure off the movie_files frame (likelihood-gated Radarr profile ladder).
SERIES are API-interleaved (per-series get_series_by_id statistics fully-downloaded guard
+ tag-freeze fetch), so the series path is split into pure phases the service threads its
fetches through: aggregate_series_signals (df → per-series signals) → active_series_candidates
(the df-based keep/recent/kids guards, run BEFORE any fetch) → [service fetches record+tags]
→ decide_series_upgrade (the record-based fully-downloaded/freeze/already-best guards + the
target-to-`best` upgrade + estimated grab). Target is the single highest-resolution profile
(`best`), matching the resolution-cap model Sonarr uses (no profile-id ladder).

Public API:
  * plan_movie_upgrades(df, ranked_profiles, *, active_cutoff, config) -> (candidates, stats)
  * aggregate_series_signals(df) -> dict[sid, signals]
  * active_series_candidates(series_data, *, cutoff, kids_certs, keep_policies) -> (active, stats)
  * series_fully_downloaded(series_record) -> bool   (apply BEFORE the tag fetch)
  * decide_series_upgrade(series_record, tag_labels, *, best_id, freeze_tags, mbpm,
        default_runtime_min=45.0) -> dict
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.machine_learning.likelihood.watch_likelihood import (
    ladder_rank,
    profile_id_for_likelihood,
    radarr_ladder,
    watch_likelihood,
)
from scripts.managers.machine_learning.sizing.size_model import estimate_gb_for_profile

# Kids certifications excluded from active-watcher upgrades (G/PG-tier).
KIDS_CERTS = {"g", "pg", "tv-g", "tv-y", "tv-y7"}

# keep_policy values the universe / keep managers own — never upgraded here.
_SKIP_POLICIES = ("keep_universe", "keep_forever", "keep_movie")


def plan_movie_upgrades(
    df,
    ranked_profiles,
    *,
    active_cutoff,
    config,
) -> "tuple[list[dict], dict]":
    """Select actively-watched movies to upgrade to their likelihood-earned profile.

    Guards (skip): no movie_id; keep_universe/keep_forever/keep_movie; kids
    certification (G/PG-tier); not actively watched (not is_watched, no last-watch,
    or last-watch older than ``active_cutoff``); already at/above the earned tier
    (target ladder rank <= current); no present profile at or below the earned rank.

    For a surviving row the target is ``profile_id_for_likelihood`` stepped DOWN the
    Radarr ladder to the first profile present in ``ranked_profiles``; reclaim is the
    NEGATIVE extra space (target size estimate minus current file size, <= 0).

    ``ranked_profiles`` is the fetched profile list (any order — indexed by id here).
    Returns ``(candidates, stats)``: each candidate carries idx / movie_id /
    target_profile / target_id / target_name / likelihood / reclaim_gb / reason; stats
    counts checked / already_best / skipped_kids / skipped_not_active (the service
    adds the apply-side upgraded / failed counters).
    """
    by_id = {p.get("id"): p for p in ranked_profiles}
    lad = radarr_ladder(config)

    candidates: "list[dict]" = []
    stats = {"checked": 0, "already_best": 0, "skipped_kids": 0, "skipped_not_active": 0}

    for idx in df.index:
        keep_policy = df.at[idx, "keep_policy"] if "keep_policy" in df.columns else None
        is_watched  = bool(df.at[idx, "is_watched"]) if "is_watched" in df.columns else False
        lw          = df.at[idx, "last_watched_at"] if "last_watched_at" in df.columns else None
        cur_qp_id   = df.at[idx, "quality_profile_id"] if "quality_profile_id" in df.columns else None
        movie_id    = df.at[idx, "movie_id"] if "movie_id" in df.columns else None
        cert        = str(df.at[idx, "certification"] or "").lower().strip() \
            if "certification" in df.columns else ""

        stats["checked"] += 1

        if pd.isna(movie_id):
            continue
        # keep_universe → universe.py owns its upgrades; keep_forever/keep_movie self-manage.
        if keep_policy in _SKIP_POLICIES:
            continue
        if cert in KIDS_CERTS:
            stats["skipped_kids"] += 1
            continue
        if not is_watched or not lw:
            stats["skipped_not_active"] += 1
            continue
        try:
            if pd.to_datetime(lw, utc=True) < active_cutoff:
                stats["skipped_not_active"] += 1
                continue
        except Exception:
            stats["skipped_not_active"] += 1
            continue

        # Likelihood-gated target via the explicit Radarr profile ladder
        # (actively-watched-once → high-1080; rewatched / high-affinity → 4K).
        # Only UPGRADE (target rank > current); never downgrade here.
        likelihood  = watch_likelihood(df.loc[idx], config=config)
        target_pid  = profile_id_for_likelihood(likelihood, config=config)
        target_rank = ladder_rank(target_pid, config=config)
        cur_rank    = ladder_rank(int(cur_qp_id), config=config) if pd.notna(cur_qp_id) else -1
        if target_rank <= cur_rank:
            stats["already_best"] += 1   # already at/above the earned tier
            continue

        target_profile = None
        for _r in range(target_rank, cur_rank, -1):   # step down to a present profile
            _pid = int(lad[_r][1])
            if _pid in by_id:
                target_profile = by_id[_pid]
                break
        if target_profile is None:
            continue
        target_id   = target_profile["id"]
        target_name = target_profile.get("name", str(target_id))

        # Estimated EXTRA space the upgrade would consume (negative reclaim = grows the
        # library). Best-effort (target size − current size) at the earned profile.
        _rt  = df.at[idx, "runtime_minutes"] if "runtime_minutes" in df.columns else None
        _szb = df.at[idx, "size_bytes"] if "size_bytes" in df.columns else None
        _cur = (float(_szb) / (1024 ** 3)) if pd.notna(_szb) else 0.0
        _tgt = (
            estimate_gb_for_profile(target_profile, float(_rt), 1)
            if _rt is not None and pd.notna(_rt) and float(_rt) > 0 else 0.0
        )

        candidates.append({
            "idx": idx,
            "movie_id": int(movie_id),
            "target_profile": target_profile,
            "target_id": target_id,
            "target_name": target_name,
            "likelihood": likelihood,
            "reclaim_gb": -max(0.0, _tgt - _cur),
            "reason": f"actively watched (L={likelihood:.0f}%) → {target_name}",
        })

    return candidates, stats


# ── series active-watcher upgrades (API-interleaved → pure phases) ─────────────────
_SERIES_SKIP_POLICIES = ("keep_series", "keep_season")


def aggregate_series_signals(df) -> "dict[int, dict]":
    """Per-series signal aggregation from the episode_files frame (pure pandas): latest
    watch time, the set of certifications, keep_policy, watched-episode + full-household
    counts, and a display title. The grain the active-watcher decision runs on."""
    series_data: "dict[int, dict]" = {}
    has_cert = "certification" in df.columns
    has_watched = "is_watched" in df.columns
    has_household = "all_household_watched" in df.columns
    for _, row in df.iterrows():
        sid = row.get("series_id")
        if pd.isna(sid):
            continue
        sid = int(sid)
        lw = row.get("last_watched_at")
        cert = str(row.get("certification") or "").lower().strip() if has_cert else ""
        policy = row.get("keep_policy") if "keep_policy" in df.columns else None
        title = row.get("series_title") or f"series {sid}"

        if sid not in series_data:
            series_data[sid] = {
                "title": title, "latest_watch": None, "certs": set(),
                "keep_policy": policy, "watched_eps": 0, "household_eps": 0,
            }
        if lw:
            try:
                dt = pd.to_datetime(lw, utc=True)
                existing = series_data[sid]["latest_watch"]
                if existing is None or dt > existing:
                    series_data[sid]["latest_watch"] = dt
            except Exception:
                pass
        if has_watched and bool(row.get("is_watched")):
            series_data[sid]["watched_eps"] += 1
        if has_household and bool(row.get("all_household_watched")):
            series_data[sid]["household_eps"] += 1
        if cert:
            series_data[sid]["certs"].add(cert)
    return series_data


def active_series_candidates(series_data, *, cutoff, kids_certs,
                             keep_policies=_SERIES_SKIP_POLICIES) -> "tuple[list, dict]":
    """The df-based guards, applied BEFORE any per-series fetch (so a skipped series costs
    no API call): keep_series/keep_season → skipped_keep; not watched within ``cutoff`` (or
    never) → skipped_not_active; kids-only (all certs in ``kids_certs``) → skipped_kids.
    Returns ``(active, stats)`` — active is the surviving ``[(sid, info), ...]`` in insertion
    order; stats has checked / skipped_keep / skipped_not_active / skipped_kids."""
    active: "list[tuple[int, dict]]" = []
    stats = {"checked": 0, "skipped_keep": 0, "skipped_not_active": 0, "skipped_kids": 0}
    for sid, info in series_data.items():
        stats["checked"] += 1
        if info["keep_policy"] in keep_policies:
            stats["skipped_keep"] += 1
            continue
        latest = info["latest_watch"]
        if latest is None or latest < cutoff:
            stats["skipped_not_active"] += 1
            continue
        certs = info["certs"]
        if certs and certs.issubset(kids_certs):
            stats["skipped_kids"] += 1
            continue
        active.append((sid, info))
    return active, stats


def series_fully_downloaded(series_record) -> bool:
    """True when every episode is already on disk (episodeFileCount >= episodeCount > 0).
    Exposed so the service can apply the fully-downloaded skip BEFORE the (API-touching)
    tag-label fetch — matching the pre-extraction ordering where fully-downloaded series
    never paid for a tag GET. ``decide_series_upgrade`` also checks it (idempotent)."""
    sb = series_record.get("statistics") or {}
    et = sb.get("episodeCount", 0) or 0
    ef = sb.get("episodeFileCount", 0) or 0
    return et > 0 and ef >= et


def decide_series_upgrade(series_record, tag_labels, *, best_id, freeze_tags, mbpm,
                          default_runtime_min: float = 45.0) -> dict:
    """The record-based decision for one active series, given its FETCHED Sonarr record +
    resolved tag-label set. Guards (in order): fully-downloaded (episodeFileCount >=
    episodeCount > 0) → ``skip='fully_downloaded'``; quality-freeze tag → ``'quality_frozen'``;
    already at the target ``best_id`` → ``'already_best'``. Otherwise ``skip=None`` plus the
    upgrade numbers (ep_total, ep_file_count, remaining, runtime_min, est_gb — the estimated
    grab of the remaining episodes at ``mbpm`` MiB/min). Pure."""
    stats_block = series_record.get("statistics") or {}
    ep_total = stats_block.get("episodeCount", 0) or 0
    ep_file_count = stats_block.get("episodeFileCount", 0) or 0
    if ep_total > 0 and ep_file_count >= ep_total:
        return {"skip": "fully_downloaded"}
    if tag_labels & freeze_tags:
        return {"skip": "quality_frozen"}
    if series_record.get("qualityProfileId") == best_id:
        return {"skip": "already_best"}
    remaining = max((ep_total or 0) - (ep_file_count or 0), 0)
    try:
        runtime_min = float(series_record.get("runtime") or 0) or default_runtime_min
    except (TypeError, ValueError):
        runtime_min = default_runtime_min
    est_gb = (mbpm * runtime_min * remaining) / 1024.0
    return {
        "skip": None, "ep_total": ep_total, "ep_file_count": ep_file_count,
        "remaining": remaining, "runtime_min": runtime_min, "est_gb": est_gb,
    }
