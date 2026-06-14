"""
affinity/group_completion.py — household movie-completion map (pure).
================================================================================
Relocated from ``services/tautulli/watch_history.get_group_movie_completions``
(ML Step 3b). For each rating group, the max completion % a movie reached across
that group's members (keyed by Plex rating_key). PURE — no HTTP, no global_cache;
the watch-history manager keeps the raw history FETCH, and the Tautulli
orchestration keeps the rating_key -> tmdb_id resolution + cache write (those
reach into the Radarr cache / registry and stay service-side).

Public API:
  * group_movie_completions(history_entries, rating_groups_cfg)
      -> {group_name: {rating_key: {"pct": float(0-1), "threshold": float}}}
"""
from __future__ import annotations


def group_movie_completions(
    history_entries: list,
    rating_groups_cfg: dict,
) -> dict:
    """For each group in *rating_groups_cfg*, compute per-movie max completion
    across all group members from *history_entries*.

    Returns ``{group_name: {rating_key: {"pct": float, "threshold": float}}}``
    where pct is 0.0-1.0 (max across all group members who watched it) and
    threshold is the applicable completion threshold for the member who achieved
    that maximum. A group with no explicit ``members`` is household-wide: every
    user counts toward it (so an unconfigured / memberless group still resolves
    completions instead of coming up empty).
    """
    # Build username -> [(group_name, threshold)] lookup; memberless groups
    # become household-wide wildcards that every user counts toward.
    user_group_info: dict[str, list[dict]] = {}
    wildcard_groups: list[dict] = []
    for group_name, group_cfg in rating_groups_cfg.items():
        members       = group_cfg.get("members", [])
        grace_members = set(group_cfg.get("grace_members", []))
        reg_threshold = group_cfg.get("completion_threshold", 0.9)
        grace_threshold = group_cfg.get("grace_threshold", 0.7)
        if not members:
            wildcard_groups.append({"group": group_name, "threshold": reg_threshold})
            continue
        for member in members:
            threshold = grace_threshold if member in grace_members else reg_threshold
            user_group_info.setdefault(member, []).append({
                "group":     group_name,
                "threshold": threshold,
            })

    group_results: dict[str, dict] = {g: {} for g in rating_groups_cfg}

    for entry in history_entries:
        if entry.get("media_type") != "movie":
            continue
        user = entry.get("user", "")
        rk   = str(entry.get("rating_key", ""))
        if not rk:
            continue
        try:
            pct = float(entry.get("percent_complete", 0)) / 100.0
        except (TypeError, ValueError):
            continue

        # Member-specific group memberships + any household-wide wildcards.
        assignments = user_group_info.get(user, [])
        if wildcard_groups:
            assignments = assignments + wildcard_groups
        for gi in assignments:
            gname     = gi["group"]
            threshold = gi["threshold"]
            existing  = group_results[gname].get(rk)
            if existing is None:
                group_results[gname][rk] = {"pct": pct, "threshold": threshold}
            elif pct > existing["pct"]:
                group_results[gname][rk] = {"pct": pct, "threshold": threshold}
            elif pct == existing["pct"] and threshold < existing["threshold"]:
                # Same pct but more lenient threshold — use it
                group_results[gname][rk] = {"pct": pct, "threshold": threshold}

    return group_results
