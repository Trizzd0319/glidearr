"""
cross_instance_dedup.py — decide which copy to keep when TWO instances own the same title (pure).
================================================================================
Under ``4k_policy == "both"`` the intended steady state is a ≤1080p BASELINE on the standard
instance PLUS a 2160p BONUS on the dedicated 4K instance. But catch-up / manual adds can leave a
title with a REDUNDANT second physical file — e.g. a 2160p copy on BOTH instances (the standard one
should never have been 4K), or two ≤1080p copies. This module decides, per title, which physical
file to KEEP and which to RECLAIM, with one inviolable rule: it only ever proposes deleting the
*worse* of two copies, so the surviving copy is always the better one and the title is never lost.

Pure: no HTTP, no config, no writes. The caller feeds the two instances' libraries (the same lists
it already fetched) and applies the returned plans through the gated actuator.

Keeper ranking (FORK 3): higher RESOLUTION wins; ties broken by TIER-MATCH (2160p belongs on the 4K
instance, ≤1080p on the standard instance), then by larger SIZE, then by newer dateAdded.

NOT a duplicate (no plan emitted):
  • only one instance owns the title, or either side has no FILE — nothing redundant to reclaim;
  • the intended dual-version split (≤1080p on standard + 2160p on the 4K instance) — that's the
    desired end state, never reclaimed (this is also exactly what a completed cross-instance MOVE
    leaves behind, so the move and the dedup never fight).

SAME-PATH duplicate (two Radarr records, ONE physical file — detected by equal ``movieFile.path``):
emitted as ``action="flag_only"``. glidearr has no filesystem access, so it CANNOT split a shared
file; deleting either record's moviefile would destroy the file the other record depends on. These
are surfaced for the operator and NEVER auto-acted.
"""
from __future__ import annotations

UHD_RES = 2160          # the 4K tier resolution
HD_MAX_RES = 1080       # a copy at/below this is a baseline-tier file


def _facts(movie: dict, inst: str) -> dict | None:
    """Per-instance facts for one title, or None when the record has no usable FILE to reason about
    (no movieFile / no file id → nothing safely reclaimable). ``res`` is the file resolution (0 when
    unparseable), ``size`` bytes (0 when unknown), ``added`` the file's dateAdded (or '' )."""
    if not isinstance(movie, dict) or not movie.get("hasFile"):
        return None
    mid = movie.get("id")
    if mid is None:
        return None                                         # no record id → can't un-monitor → skip
    mf = movie.get("movieFile") or {}
    fid = mf.get("id")
    if fid is None:
        return None
    q = ((mf.get("quality") or {}).get("quality") or {})
    try:
        res = int(q.get("resolution")) if q.get("resolution") is not None else 0
    except (TypeError, ValueError):
        res = 0
    try:
        size = int(mf.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    path = str(mf.get("path") or "").replace("\\", "/").strip().rstrip("/")
    return {"inst": inst, "movie_id": mid, "file_id": fid, "res": res, "size": size,
            "path": path, "added": str(mf.get("dateAdded") or movie.get("added") or ""),
            "title": movie.get("title")}


def _tier_match(fact: dict, std_inst: str, uhd_inst: str) -> int:
    """1 when the file sits on the instance its resolution belongs to (2160p→4K instance,
    ≤1080p→standard instance), else 0. The same-resolution tie-breaker."""
    if fact["res"] >= UHD_RES:
        return 1 if fact["inst"] == uhd_inst else 0
    return 1 if fact["inst"] == std_inst else 0


def _rank(fact: dict, std_inst: str, uhd_inst: str) -> tuple:
    """Keeper ranking key (higher is better): resolution, then tier-match, then size, then recency."""
    return (fact["res"], _tier_match(fact, std_inst, uhd_inst), fact["size"], fact["added"])


def _is_intended_dual_version(std_f: dict, uhd_f: dict) -> bool:
    """The desired ``both`` end state — a ≤1080p baseline on standard AND a 2160p bonus on the 4K
    instance — is NOT a duplicate to reclaim."""
    return std_f["res"] <= HD_MAX_RES and uhd_f["res"] >= UHD_RES


def plan_dedup(std_inst: str, std_movies, uhd_inst: str, uhd_movies) -> list[dict]:
    """Return one plan per REDUNDANT title across the two instances. Each plan is either
    ``action="reclaim_loser_file"`` (delete the worse copy's FILE; the better copy is the keeper) or
    ``action="flag_only"`` (a same-path duplicate the operator must resolve). Titles that are not
    duplicates emit nothing."""
    std_by_tmdb = {}
    for m in (std_movies or []):
        f = _facts(m, std_inst)
        if f is not None and m.get("tmdbId") is not None:
            std_by_tmdb[m.get("tmdbId")] = f
    plans: list[dict] = []
    for m in (uhd_movies or []):
        tmdb = m.get("tmdbId")
        if tmdb is None or tmdb not in std_by_tmdb:
            continue
        uhd_f = _facts(m, uhd_inst)
        if uhd_f is None:
            continue                                        # 4K side has no usable file → nothing to dedup
        std_f = std_by_tmdb[tmdb]
        title = std_f.get("title") or uhd_f.get("title")

        # Two records, one physical file → can't split over the API; surface, never act.
        if std_f["path"] and std_f["path"] == uhd_f["path"]:
            plans.append({"tmdb": tmdb, "title": title, "is_same_path": True, "action": "flag_only",
                          "path": std_f["path"], "std_inst": std_inst, "uhd_inst": uhd_inst,
                          "reason": "two records share one physical file — operator must resolve on disk"})
            continue

        # The intended dual-version split is the desired end state, not a duplicate.
        if _is_intended_dual_version(std_f, uhd_f):
            continue

        keeper, loser = (uhd_f, std_f) if _rank(uhd_f, std_inst, uhd_inst) >= _rank(
            std_f, std_inst, uhd_inst) else (std_f, uhd_f)
        plans.append({
            "tmdb": tmdb, "title": title, "is_same_path": False, "action": "reclaim_loser_file",
            "keeper_inst": keeper["inst"], "keeper_movie_id": keeper["movie_id"],
            "keeper_file_id": keeper["file_id"], "keeper_res": keeper["res"],
            "loser_inst": loser["inst"], "loser_movie_id": loser["movie_id"],
            "loser_file_id": loser["file_id"], "loser_res": loser["res"],
            "reason": (f"keep {keeper['res'] or '?'}p on {keeper['inst']}, "
                       f"reclaim {loser['res'] or '?'}p on {loser['inst']}"),
        })
    return plans
