"""
dual_version.py — plan the HD second copy for a dual-version (4K + HD) movie (pure).
================================================================================
When ``routing.movies.4k_policy == "both"`` a UHD movie keeps its 4K copy AND a smaller HD
(<=1080p) copy on the standard instance, so remote / low-bandwidth Plex clients direct-play
the HD file instead of transcoding the 4K. This module decides — purely — WHETHER and with
WHICH quality profile to acquire that second copy. It is consulted POST-FILE (once the 4K
file has landed and is confirmed UHD), not at add time.

Guards:
  • only when 4k_policy == "both", the title is UHD, and it is not already on the HD instance;
  • score-gated by ``4k_dual_min_score`` (0 = always);
  • the HD copy uses an EXISTING <=1080p profile — never one that allows 2160p (which would
    silently make a SECOND 4K copy); with no HD-capped profile it skips, with a reason.
Removing a redundant copy under ``highest_only`` is a separate, deferred (log-only) concern.

PURE — no HTTP, no config object, no acquisition; the caller supplies the candidate's facts +
the HD instance/root/profiles and acts on the returned plan.
"""
from __future__ import annotations

from scripts.support.utilities.size_model import profile_max_quality

HD_MAX_RES = 1080      # the HD copy must not exceed this, else it is just a second 4K copy


def pick_hd_profile(profiles):
    """The highest-quality quality profile whose MAX allowed resolution is <= HD_MAX_RES, so
    the HD copy stays 1080p and never chases 4K. Returns the profile dict, or ``None`` when
    every available profile would allow more than 1080p (the caller then skips the second copy
    rather than create a redundant 4K one)."""
    eligible = []
    for p in (profiles or []):
        try:
            max_res, _ = profile_max_quality(p)
        except Exception:
            continue
        if max_res is not None and 0 < int(max_res) <= HD_MAX_RES:
            eligible.append((int(max_res), p))
    if not eligible:
        return None
    eligible.sort(key=lambda t: t[0])
    return eligible[-1][1]                  # the highest-quality profile still <=1080p


def plan_hd_copy(*, tmdb, title, is_uhd, score, routing, hd_profiles, hd_instance, hd_root,
                 already_on_hd=False):
    """Decide whether to acquire a second HD copy of a UHD movie for remote play. Returns
    ``(plan | None, reason)``; a plan is ``{tmdb, title, instance, root_folder, profile}``."""
    mv = (routing or {}).get("movies", {}) or {}
    if mv.get("4k_policy") != "both":
        return None, "4k_policy != both"
    if not is_uhd:
        return None, "not a UHD title — no second copy needed"
    if already_on_hd:
        return None, "HD copy already present"
    try:
        min_score = int(mv.get("4k_dual_min_score", 0) or 0)
    except (TypeError, ValueError):
        min_score = 0
    if score is not None:
        try:
            if int(score) < min_score:
                return None, f"score {score} < min {min_score}"
        except (TypeError, ValueError):
            pass
    if not hd_instance or not hd_root:
        return None, "no HD instance / root folder configured"
    profile = pick_hd_profile(hd_profiles)
    if profile is None:
        return None, "no <=1080p quality profile on the HD instance — refusing to make a second 4K copy"
    return ({"tmdb": tmdb, "title": title, "instance": hd_instance,
             "root_folder": hd_root, "profile": profile},
            f"queue HD copy ({profile.get('name', '?')}) for remote play")
