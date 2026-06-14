"""
plex/playlists/tv_resolver.py — join owned episodes → Plex ratingKeys → a TV plan.
================================================================================
The SERVICE glue between the data foundation and the pure brain. Pure given its
inputs (no I/O here — the manager wrapper loads the caches/parquets and passes
scalars), so it is fully unit-testable:

  owned episodes (sonarr/cache/owned_episodes, carrying tvdb_join_key)
    ⋈ plex/episodes/owned_inventory   (tvdb_join_key → Plex episode ratingKey)
    ⋈ per-user watched ratingKeys     (Tautulli per-user history)
    ⋈ per-series watchability score
  → PlaylistInput[] → expand_show (cap per series) → order_items (brain)
  → PlaylistPlan (+ resolution stats).

Episodes that don't resolve to a ratingKey are dropped and COUNTED (never guessed) —
the brain only ever sees resolvable, playable items.
"""
from __future__ import annotations

from scripts.managers.machine_learning.playlists.expansion import NEXT_UNWATCHED, expand_show
from scripts.managers.machine_learning.playlists.models import PlaylistInput
from scripts.managers.machine_learning.playlists.ordering import order_items


def _norm(s) -> str:
    return str(s or "").strip().lower()


def _coerce_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def watched_episode_keys(history: list, *, min_pct: float = 85.0) -> set:
    """Per-user finished-episode IDENTITIES from Tautulli history (``percent_complete
    >= min_pct``; an in-progress episode stays a candidate so 'Up Next' can resume it).

    Returns a set MIXING three identity kinds so the join against the owned inventory is
    robust:
      • the episode ratingKey (``str``) — exact, when Plex hasn't re-scanned;
      • a ``(series, season, episode)`` tuple — numeric, the most reliable; and
      • a ``(series, episode_title)`` tuple — fallback when indices are missing.

    Tautulli records the ratingKey *as it was at play time*, so a Plex library re-scan /
    re-match / duplicate leaves the historical ratingKey pointing at a now-stale item.
    Observed on a real heavy watcher: only 11/117 of one show's watched episodes still
    matched by ratingKey, but 117/117 matched by (series, season, episode). Matching on
    ANY identity recovers the rest while keeping the exact-ratingKey path for fresh data."""
    out: set = set()
    for row in history or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("media_type", "")).lower() != "episode":
            continue
        try:
            pct = float(row.get("percent_complete") or 0)
        except (TypeError, ValueError):
            pct = 0.0
        if pct < min_pct:
            continue
        rk = row.get("rating_key")
        if rk is not None:
            out.add(str(rk))
        st = _norm(row.get("grandparent_title"))
        season = _coerce_int(row.get("parent_media_index"))
        episode = _coerce_int(row.get("media_index"))
        if st and season is not None and episode is not None:
            out.add((st, season, episode))
        tt = _norm(row.get("title"))
        if st and tt:
            out.add((st, tt))
    return out


def tv_inputs(owned_eps: list, owned_inventory: dict, watched, series_scores: dict,
              *, episode_cap: int = 25, mode: str = NEXT_UNWATCHED):
    """Resolve owned episodes to Plex ratingKeys + expand each owned series to its
    next-unwatched-by-this-user episodes (capped) — the candidate ``PlaylistInput``s
    WITHOUT ordering. Shared by :func:`build_tv_plan` and the combined cross-medium plan.
    Returns ``(list[PlaylistInput], stats)``.

    ``watched`` is the mixed identity set from :func:`watched_episode_keys` (ratingKeys
    + ``(series, title)`` tuples). An episode is treated as watched if EITHER its
    ratingKey OR its ``(series_title, episode_title)`` is in the set — ratingKey alone
    silently misses episodes whose Plex item was re-scanned (the watched filter then
    restarts a show the user finished from S1E1)."""
    watched = set(watched or ())
    inv = owned_inventory or {}
    scores = series_scores or {}
    stats = {"owned": len(owned_eps or []), "resolved": 0, "unresolved": 0}

    by_series: dict = {}
    for ep in owned_eps or []:
        jk = ep.get("tvdb_join_key")
        match = inv.get(jk) if jk else None
        rk = str(match["rating_key"]) if (match and match.get("rating_key")) else None
        if rk is None:
            stats["unresolved"] += 1          # owned but not matchable on this Plex server
            continue
        stats["resolved"] += 1
        sid = ep.get("series_id")
        # Stable-identity watched check: ratingKey (fresh data) OR (series, season,
        # episode) OR (series, title) — the tuple identities survive Plex re-scans /
        # duplicates that churn episode ratingKeys (ratingKey alone restarts a finished
        # show from S1E1).
        s_title, e_title = _norm(match.get("series_title")), _norm(match.get("title"))
        season, episode = _coerce_int(ep.get("season_number")), _coerce_int(ep.get("episode_number"))
        is_watched = (
            rk in watched
            or (s_title and season is not None and episode is not None
                and (s_title, season, episode) in watched)
            or (s_title and e_title and (s_title, e_title) in watched)
        )
        by_series.setdefault(sid, []).append(PlaylistInput(
            rating_key=rk, medium="episode", series_id=sid,
            season=ep.get("season_number"), episode=ep.get("episode_number"),
            title=ep.get("title") or (match.get("title") or ""),
            air_date=ep.get("air_date_utc"),
            is_special=bool(ep.get("is_special")),
            score=scores.get(sid),
            watched=is_watched,
        ))

    expanded: list = []
    for eps in by_series.values():
        expanded.extend(expand_show(eps, mode=mode, cap=episode_cap))
    stats["series"] = len(by_series)
    return expanded, stats


def build_tv_plan(owned_eps: list, owned_inventory: dict, watched, series_scores: dict,
                  *, family: str = "up_next", episode_cap: int = 25, max_items: int = 300,
                  mode: str = NEXT_UNWATCHED):
    """Resolve + expand owned episodes (:func:`tv_inputs`) then hand them to the brain to
    order. Returns ``(PlaylistPlan, stats)``."""
    expanded, stats = tv_inputs(owned_eps, owned_inventory, watched, series_scores,
                                episode_cap=episode_cap, mode=mode)
    plan = order_items(expanded, family=family, max_items=max_items)
    stats["in_plan"] = len(plan.items)
    return plan, stats
