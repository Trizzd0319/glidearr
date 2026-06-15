"""
dual_version.py — dual-version (1080p baseline + 4K bonus) decisions for movies (pure).
================================================================================
When ``routing.movies.4k_policy == "both"`` a movie is kept as a 1080p copy on the standard
instance PLUS, when warranted, a 4K copy on the 4K instance.

Polarity (important): **1080p is the PRIORITY baseline — always present — and 4K is the BONUS
layer on top.** The 1080p copy is the durable, remote-play-friendly floor that any client can
direct-play; the 4K copy is the premium-local upgrade.

  • HD baseline (``plan_hd_baseline``): ensure a 1080p copy exists. Resolution is SCORE/SPACE-
    adaptive but hard-capped at 1080p (a high-value title gets 1080p, a lower one 720p/480p; the
    caller can push ``res_cap`` lower under disk pressure). It never exceeds 1080p.
  • 4K bonus (``wants_uhd``): added ONLY when space allows AND the title is keep/universe-tagged,
    OR its watchability is high enough to warrant UHD (and a viewer can make use of it). Otherwise
    1080p alone.
  • Eviction (``should_drop_uhd``): under pressure the 4K copy is the FIRST to go — when free space
    fell below the floor, or the title no longer warrants UHD. The 1080p baseline is always kept,
    and the caller must ensure it EXISTS before removing the 4K (make-before-break — never a gap).

The ACQUISITION of the second add and the EVICTION ordering (1080p-before-4K-removal, 4K-before
other deletions) are enforced by the acquisition / space-coordinator integration using these
decisions. This module is PURE — no HTTP, no config object; the caller supplies the facts +
the space/viewer signals + the HD instance/root/profiles.
"""
from __future__ import annotations

from scripts.support.utilities.size_model import profile_max_quality, target_resolution_for_score

HD_MAX_RES = 1080            # the HD baseline never exceeds this (else it is just a second 4K copy)
DEFAULT_UHD_SCORE = 70       # watchability at/above which a title warrants the 4K bonus (mirrors
                             # watch_likelihood.uhd_cutoff); override per-deployment via config.


def hd_target_resolution(score) -> int:
    """The non-4K resolution tier the score justifies — 1080p OR 720p (or 480p) 'whichever is
    justified from elsewhere', NOT a hardcoded 1080. Reuses the SAME shared ladder the resolver
    uses for the primary copy (``size_model.target_resolution_for_score``) and clamps it below 4K
    (<= 1080), so a high-value title gets 1080p, a mid one 720p, a low one 480p — never 2160. A
    title with no score yet gets the full 1080 cap (the baseline must always be a watchable copy),
    not the ladder's low default."""
    if score is None:
        return HD_MAX_RES
    return min(target_resolution_for_score(score), HD_MAX_RES)


def pick_hd_profile(profiles, score=None, res_cap: int = HD_MAX_RES):
    """Highest-quality profile whose MAX allowed resolution is <= the effective cap
    ``min(score-tier, res_cap, 1080)`` — so the baseline scales with the score, can be pushed
    lower by the caller under disk pressure (``res_cap``), and NEVER exceeds 1080p. ``None`` when
    no profile fits under the cap (the caller then skips)."""
    cap = min(hd_target_resolution(score), int(res_cap or HD_MAX_RES), HD_MAX_RES)
    eligible = []
    for p in (profiles or []):
        try:
            max_res, _ = profile_max_quality(p)
        except Exception:
            continue
        if max_res is not None and 0 < int(max_res) <= cap:
            eligible.append((int(max_res), p))
    if not eligible:
        return None
    eligible.sort(key=lambda t: t[0])
    return eligible[-1][1]


def wants_uhd(*, keep_tagged, score, space_allows, uhd_threshold: int = DEFAULT_UHD_SCORE,
             can_remote_play: bool = True) -> bool:
    """Whether the 4K (bonus) copy is warranted ON TOP of the 1080p baseline. True only when
    space allows AND the title is keep/universe-tagged, OR its watchability is high enough
    (``score >= uhd_threshold``) and a viewer can make use of it (``can_remote_play``). Space is
    required either way — the 4K is a bonus, never at the baseline's expense."""
    if not space_allows:
        return False
    if keep_tagged:
        return True
    try:
        high = score is not None and int(score) >= int(uhd_threshold)
    except (TypeError, ValueError):
        high = False
    return high and bool(can_remote_play)


def should_drop_uhd(*, keep_tagged, score, free_below_floor,
                    uhd_threshold: int = DEFAULT_UHD_SCORE) -> bool:
    """Whether to remove the 4K (bonus) copy — the FIRST thing to go under pressure. True when
    free space fell below the floor, or the title no longer warrants UHD (not keep-tagged AND
    watchability dropped below the threshold). The 1080p baseline is always kept; the caller must
    ensure it exists BEFORE removing the 4K (make-before-break, never a gap)."""
    if free_below_floor:
        return True
    if keep_tagged:
        return False
    try:
        return score is None or int(score) < int(uhd_threshold)
    except (TypeError, ValueError):
        return True


def plan_hd_baseline(*, tmdb, title, routing, hd_profiles, hd_instance, hd_root, score=None,
                     already_present=False, res_cap: int = HD_MAX_RES):
    """Ensure the 1080p baseline copy exists (the PRIORITY, remote-play-friendly copy). Returns
    ``(plan | None, reason)``; a plan is ``{tmdb, title, instance, root_folder, profile}``. The
    resolution is score/space-adaptive, capped at 1080. Only relevant under 4k_policy == "both"."""
    mv = (routing or {}).get("movies", {}) or {}
    if mv.get("4k_policy") != "both":
        return None, "4k_policy != both (no separate HD baseline)"
    if already_present:
        return None, "1080p baseline already present"
    if not hd_instance or not hd_root:
        return None, "no HD instance / root folder configured"
    profile = pick_hd_profile(hd_profiles, score, res_cap)
    if profile is None:
        return None, "no quality profile under the HD cap"
    return ({"tmdb": tmdb, "title": title, "instance": hd_instance,
             "root_folder": hd_root, "profile": profile},
            f"queue 1080p baseline ({profile.get('name', '?')})")
