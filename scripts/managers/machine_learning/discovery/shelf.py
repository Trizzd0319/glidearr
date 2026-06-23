"""Per-user 'This Week in History' shelf assembly — PURE. Given the run-scoped, already-scored
anniversary candidate pools (household watchability is user-independent, so scoring happens ONCE),
this age-gates each user's view (fail-CLOSED) and splits the survivors into:
  * a PLAYLIST of OWNED titles resolved to Plex ratingKeys (the free fallback that can ship in a
    read-only Phase 2), capped + watchability-ordered; TV resolves to the PILOT (entry point), else
    the matched anniversary episode;
  * a PREVIEW of NET-NEW (unowned) finds — what acquisition WOULD grab in Phase 3 (no ratingKey, no
    grab here).
The library-scope gate is applied by the caller (it passes an empty pool for a medium the user can't
access). No I/O — the inventories + the age tier are inputs.
"""
from __future__ import annotations

from scripts.managers.machine_learning.playlists.cert_gate import cert_allowed, is_restricted


def _age_ok(cand, level) -> bool:
    """Fail-CLOSED age gate: an unrestricted profile sees everything; a restricted profile keeps a
    candidate only when its certification (CSM-age fallback) fits the tier."""
    if not is_restricted(level):
        return True
    return cert_allowed(cand.get("certification"), level, csm_age=cand.get("csm_age"))


def movie_resolver(movie_inv):
    """tmdb candidate → owned-movie ratingKey via ``plex/movies/owned_inventory`` (str(tmdb) → rk)."""
    inv = movie_inv or {}

    def resolve(c):
        m = inv.get(str(c.get("tmdb_id")))
        return m.get("rating_key") if isinstance(m, dict) else None
    return resolve


def show_resolver(episode_inv):
    """show candidate → owned-episode ratingKey via ``plex/episodes/owned_inventory`` (``{tvdb}:{s}:{e}``
    → rk). The entry point is the PILOT (S1E1); if that episode isn't owned, fall back to the matched
    anniversary episode."""
    inv = episode_inv or {}

    def resolve(c):
        tvdb = c.get("tvdb_id")
        for key in (f"{tvdb}:1:1", f"{tvdb}:{c.get('season')}:{c.get('episode')}"):
            m = inv.get(key)
            if isinstance(m, dict) and m.get("rating_key"):
                return m.get("rating_key")
        return None
    return resolve


def _title(c):
    return c.get("title") or c.get("series_title") or ""


def gated_plan(scored, *, level, cap, resolve):
    """``(owned_items, net_new_rows)`` from a PRE-SCORED (watchability-desc) candidate pool. Age-gates
    fail-CLOSED, then walks best-first: an OWNED candidate that resolves to a ratingKey becomes a
    capped playlist item; an unowned candidate becomes a capped preview row. ``scored`` items carry
    ``score``/``why`` (from the scorer) and ``certification``/``csm_age`` (attached before scoring)."""
    owned_items: list = []
    net_new: list = []
    for c in scored:
        if not _age_ok(c, level):
            continue
        if c.get("owned"):
            if len(owned_items) >= cap:
                continue
            rk = resolve(c)
            if rk is None:
                continue
            owned_items.append({
                "rating_key": str(rk), "ordinal": len(owned_items), "score": c.get("score"),
                "reason": c.get("why", ""), "title": _title(c), "years_ago": c.get("years_ago"),
            })
        elif len(net_new) < cap:
            net_new.append({
                "title": _title(c), "media": c.get("media"), "years_ago": c.get("years_ago"),
                "score": c.get("score"), "why": c.get("why", ""), "owned": False,
                "tmdb_id": c.get("tmdb_id"), "tvdb_id": c.get("tvdb_id"),
            })
    return owned_items, net_new
