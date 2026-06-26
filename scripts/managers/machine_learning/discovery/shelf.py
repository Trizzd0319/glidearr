"""Per-user 'This Week in History' shelf assembly — PURE. Given the run-scoped, already-scored
anniversary candidate pools (household watchability is user-independent, so scoring happens ONCE),
this age-gates each user's view (fail-CLOSED) and splits the survivors into:
  * a PLAYLIST of OWNED titles resolved to Plex ratingKeys (the free fallback that can ship in a
    read-only Phase 2), capped + watchability-ordered; TV resolves to the PILOT (entry point), else
    the matched anniversary episode;
  * a PREVIEW of NET-NEW (unowned) finds — what acquisition WOULD grab in Phase 3 (no ratingKey, no
    grab here).
The library-scope gate is per-section: the caller builds the resolver with that user's ``allowed``
section set, so an owned title resolves only from a library the user was actually shared (fail-CLOSED;
a SUBSET grant yields a scoped shelf, not nothing). No I/O — the inventories + the age tier are inputs.
"""
from __future__ import annotations

from scripts.managers.machine_learning.playlists.cert_gate import cert_allowed, is_restricted
from scripts.managers.machine_learning.playlists.per_user import genre_match, priority_score


def personalize(scored, user_aff, *, hh_max, weights, gm_opts=None):
    """Re-rank a HOUSEHOLD-scored candidate pool by ONE user's genre affinity — the same
    ``priority_score`` blend (affinity > household; no JIT for a trial shelf) the other per-user
    playlists use, so a viewer's anniversary picks reflect THEIR taste, not just household watchability.
    Returns new candidate copies whose ``score`` is the per-user blend, sorted desc. With no affinity
    (unmatched profile / cold start) the household order is preserved unchanged. PURE."""
    if not user_aff:
        return list(scored)
    aff_w, hh_w, jit_w = weights
    gm_opts = gm_opts or {}
    out = []
    for c in scored:
        base = c.get("score")
        hh_norm = (float(base) / hh_max) if (isinstance(base, (int, float)) and hh_max) else 0.0
        gm = genre_match(c.get("genres") or [], user_aff, **gm_opts)
        ps = priority_score(hh_norm, gm, is_jit=False, affinity_weight=aff_w,
                            jit_weight=jit_w, household_weight=hh_w)
        out.append({**c, "score": round(ps * 100, 1)})
    out.sort(key=lambda c: c.get("score") or 0.0, reverse=True)
    return out


def _age_ok(cand, level) -> bool:
    """Fail-CLOSED age gate: an unrestricted profile sees everything; a restricted profile keeps a
    candidate only when its certification (CSM-age fallback) fits the tier."""
    if not is_restricted(level):
        return True
    return cert_allowed(cand.get("certification"), level, csm_age=cand.get("csm_age"))


def _section_ok(entry, allowed) -> bool:
    """Per-section library gate (fail-CLOSED). ``allowed=None`` → no filter (the household preview,
    which is not user-scoped). Otherwise the owned item resolves ONLY when its recorded source
    ``section`` is in the user's allowlist; a missing/un-granted section is excluded."""
    if allowed is None:
        return True
    sec = entry.get("section")
    return sec is not None and str(sec) in allowed


def movie_resolver(movie_inv, allowed=None):
    """tmdb candidate → owned-movie ratingKey via ``plex/movies/owned_inventory`` (str(tmdb) → rk).
    With ``allowed`` (a set of section keys), an owned movie resolves only from a granted library —
    so a viewer shared a SUBSET of movie sections gets a properly-scoped shelf, fail-CLOSED."""
    inv = movie_inv or {}

    def resolve(c):
        m = inv.get(str(c.get("tmdb_id")))
        if not (isinstance(m, dict) and _section_ok(m, allowed)):
            return None
        return m.get("rating_key")
    return resolve


def show_resolver(episode_inv, allowed=None):
    """show candidate → owned-episode ratingKey via ``plex/episodes/owned_inventory`` (``{tvdb}:{s}:{e}``
    → rk). The entry point is the PILOT (S1E1); if that episode isn't owned, fall back to the matched
    anniversary episode. With ``allowed``, only episodes from a granted section resolve (fail-CLOSED)."""
    inv = episode_inv or {}

    def resolve(c):
        tvdb = c.get("tvdb_id")
        for key in (f"{tvdb}:1:1", f"{tvdb}:{c.get('season')}:{c.get('episode')}"):
            m = inv.get(key)
            if isinstance(m, dict) and m.get("rating_key") and _section_ok(m, allowed):
                return m.get("rating_key")
        return None
    return resolve


def _title(c):
    return c.get("title") or c.get("series_title") or ""


def gated_plan(scored, *, level, cap, resolve, seen=None):
    """``(owned_items, net_new_rows)`` from a PRE-SCORED (watchability-desc) candidate pool. Age-gates
    fail-CLOSED, then walks best-first: an OWNED candidate that resolves to a ratingKey becomes a
    capped playlist item; an unowned candidate becomes a capped preview row. ``scored`` items carry
    ``score``/``why`` (from the scorer) and ``certification``/``csm_age`` (attached before scoring).

    The owned picks are ordered by NOVELTY into two tiers: owned-but-UNWATCHED first (rediscovery — a
    forgotten gem), then already-SEEN at the BOTTOM (an anniversary REWATCH prompt — "you saw this; it's
    its anniversary"). ``seen(cand, rk) -> bool`` routes the tier: for a MOVIE it's "finished this movie";
    for a SHOW it's SERIES-level — watched ANY owned episode (so a show you're mid-way through demotes even
    when the surfaced pilot isn't the watched episode). ``None`` → nothing seen (fail-OPEN: a profile with
    no Tautulli match → everything treated unwatched). Each owned item carries ``seen``.

    CAP: ``cap`` bounds only the NET-NEW (discovery) picks — those cost a grab/budget, so the shelf curates
    them; conceptually they sit ABOVE both owned tiers. The OWNED tiers are UNCAPPED (free, no grab): every
    owned anniversary title is included, watchability-desc within its tier."""
    seen = seen or (lambda c, rk: False)
    unwatched: list = []
    seen_items: list = []
    net_new: list = []
    for c in scored:
        if not _age_ok(c, level):
            continue
        if c.get("owned"):
            rk = resolve(c)
            if rk is None:                              # out-of-scope library → not on the shelf at all
                continue
            is_seen = bool(seen(c, rk))
            (seen_items if is_seen else unwatched).append({
                "rating_key": str(rk), "score": c.get("score"), "reason": c.get("why", ""),
                "title": _title(c), "years_ago": c.get("years_ago"), "seen": is_seen,
                "on_this_day": bool(c.get("on_this_day")),
            })
        else:
            net_new.append({
                "title": _title(c), "media": c.get("media"), "years_ago": c.get("years_ago"),
                "score": c.get("score"), "why": c.get("why", ""), "owned": False,
                "tmdb_id": c.get("tmdb_id"), "tvdb_id": c.get("tvdb_id"),
                "on_this_day": bool(c.get("on_this_day")),
                # Carried for Phase-3 demand ordering of net-new grabs (else demand_score sees no
                # genres/votes and silently degrades to score order).
                "genres": list(c.get("genres") or []), "votes": c.get("votes"),
            })
    # The NET-NEW (grab) picks lead with a title whose anniversary is EXACTLY today ("on this very day"),
    # then watchability — that calendar hook is what justifies spending a grab on it; the cap is applied
    # AFTER so today's finds make the cut. The OWNED tiers sort by per-user watchability ALONE: each row
    # keeps its on_this_day flag (for an "anniversary today" badge) but it no longer forces the top, so a
    # viewer's strong-affinity owned pick outranks a low-affinity title that merely falls on today — the
    # owned shelf stays personalized even on a week with many on-this-day owned titles (a hard today-first
    # tier would otherwise freeze an identical top slice for every user).
    net_new.sort(key=_today_then_score, reverse=True)
    for grp in (unwatched, seen_items):
        grp.sort(key=_score_only, reverse=True)
    net_new = net_new[:cap]
    owned_items = unwatched + seen_items                # already-seen anniversary titles sit at the BOTTOM
    for i, item in enumerate(owned_items):
        item["ordinal"] = i
    return owned_items, net_new


def _score_only(item):
    return item.get("score") if item.get("score") is not None else 0.0


def _today_then_score(item):
    return (1 if item.get("on_this_day") else 0, _score_only(item))
