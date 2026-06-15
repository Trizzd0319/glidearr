"""
library_router.py — apply routing preferences + plan owned-library re-org moves (pure).
================================================================================
Two pure pieces shared by the add-time resolver and the in-run re-organizer, so both
make IDENTICAL decisions:

  • route_category(category, is_show, routing) — redirect a CLASSIFIED library bucket to
    its EFFECTIVE folder bucket per the operator's ``routing`` preferences. The classifier
    decides WHAT a title is; this decides WHERE it goes given the prefs (kids/anime toggles).

  • plan_moves(items, …) — for each owned *arr item, classify → route → compare its current
    root folder to the configured destination, emitting a MovePlan when they differ (and, for
    shows, a seriesType correction). SAME-instance folder moves only; cross-instance migration
    (anime / 4K instance) is a separate, deferred concern.

PURE — no HTTP, no config object, no logging. The caller injects the ``classify`` callable
and the configured folder maps.
"""
from __future__ import annotations


def route_category(category: str, is_show: bool, routing: dict) -> str:
    """Effective library bucket for FOLDER routing after applying ``routing`` prefs. Redirects
    only when a bucket is turned OFF — otherwise returns the classified category unchanged:
      • movie kids  → standard  when routing.movies.kids_bucket_enabled is off
      • movie anime → standard  when routing.movies.anime_policy == "standard_only"
      • show  anime → series    when routing.tv.anime_policy == "series_type"
      • show  kids  → series    when routing.tv.kids_bucket_enabled is off
    seriesType is tracked separately, so a series_type anime still parses as anime — it just
    lands in the series folder rather than a dedicated anime one. The caller decides WHETHER to
    apply these (e.g. only once routing.configured is stamped)."""
    mv = (routing or {}).get("movies", {}) or {}
    tv = (routing or {}).get("tv", {}) or {}
    if is_show:
        if category == "anime" and tv.get("anime_policy") == "series_type":
            return "series"
        if category == "kids" and not tv.get("kids_bucket_enabled", True):
            return "series"
    else:
        if category == "kids" and not mv.get("kids_bucket_enabled", True):
            return "standard"
        if category == "anime" and mv.get("anime_policy") == "standard_only":
            return "standard"
    return category


def target_folder(eff_category: str, is_show: bool, root_folders: dict, movie_root_folders: dict) -> str:
    """Configured destination folder for an effective category (mirrors resolver._pick_root_folder):
    the category's own folder, else the default bucket (series for shows, standard for movies)."""
    if is_show:
        rf = root_folders or {}
        return rf.get(eff_category) or rf.get("series") or ""
    mrf = movie_root_folders or {}
    return mrf.get(eff_category) or mrf.get("standard") or ""


def _norm(p) -> str:
    """Normalise a folder path for comparison: forward slashes, no trailing slash, lower-cased."""
    return str(p or "").replace("\\", "/").rstrip("/").lower()


def plan_moves(items, *, is_show, routing, root_folders, movie_root_folders, classify, anime_media=None) -> list:
    """For each owned *arr item (a dict carrying at least id/title/rootFolderPath plus the
    classification inputs), classify it (``classify(item) -> category``), apply the routing prefs,
    compute the configured destination folder, and emit a MovePlan when the item's current root
    folder differs. For shows, ``anime_media(item) -> bool`` drives a seriesType correction (anime
    parsing kept even when the title is routed to a non-anime folder). SAME-instance moves only.

    A MovePlan is ``{id, title, category, eff_category, current_root, target_root, new_series_type,
    reason}``; ``target_root`` is None when only a seriesType fix is needed (no folder move)."""
    plans = []
    for it in items:
        cat = classify(it)
        eff = route_category(cat, is_show, routing)
        target = target_folder(eff, is_show, root_folders, movie_root_folders)
        cur = it.get("rootFolderPath") or it.get("path") or ""
        needs_move = bool(target) and _norm(cur) != _norm(target)

        new_stype = None
        if is_show and anime_media is not None:
            cur_stype = (it.get("seriesType") or "standard").strip().lower()
            if anime_media(it):
                desired = "anime"
            elif cur_stype == "anime":
                desired = "standard"          # a mistyped non-anime series → correct it
            else:
                desired = None
            if desired and desired != cur_stype:
                new_stype = desired

        if needs_move or new_stype:
            why = []
            if needs_move:
                why.append(f"{cat}->{eff} folder" if cat != eff else f"{eff} folder")
            if new_stype:
                why.append(f"seriesType {(it.get('seriesType') or 'standard')}->{new_stype}")
            plans.append({
                "id": it.get("id"), "title": it.get("title"),
                "category": cat, "eff_category": eff,
                "current_root": cur, "target_root": target if needs_move else None,
                "new_series_type": new_stype, "reason": "; ".join(why),
            })
    return plans
