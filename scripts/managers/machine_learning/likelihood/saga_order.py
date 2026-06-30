"""
likelihood/saga_order.py — the PURE cross-media saga-axis primitives (brain layer).
================================================================================
``unified_universe_order`` collapses a universe's films + shows onto ONE saga axis;
``saga_member_engagement`` turns that axis + the household-watched ids into the per-member
caught-up / depth engagement that ``watch_likelihood.saga_credit`` keys the saga QUALITY credit off.

These were extracted from ``services/plex/playlists/universe_order.py`` (which RE-EXPORTS them, so its
existing service callers — the acquisition capstone, the order tests — import them unchanged) because
the brain (``machine_learning/``) must not import the service layer: ``likelihood/saga_engagement.py``
consumes them and is brain-pure. The math is deterministic and self-contained — no I/O, no service /
HTTP / cache imports (brain_purity-guarded).
"""
from __future__ import annotations


def unified_universe_order(source, owned_movie_tmdbs, owned_tvdb_to_sid, *, include_unowned=False):
    """A universe's films + shows on ONE saga axis: ``{universe_key: [{media, id, rank, owned}…]}``,
    densely re-ranked over the source's unified ``items`` list (``split_list_media``). This is
    the cross-media order the per-media ``build_universe_maps`` can't express (it numbers movies
    and shows on independent axes).

    ``id`` is source-native — ``tmdb`` for a movie, ``tvdb`` for a show — so the caller resolves
    owned→ratingKey/series_id (playlist) or acquires by id (Radarr by tmdb / Sonarr by tvdb). A
    movie is ``owned`` when its tmdb is in ``owned_movie_tmdbs``; a show when its tvdb is in
    ``owned_tvdb_to_sid``. ``include_unowned`` False (playlist) → only owned survivors; True
    (acquisition) → keeps the saga's gaps flagged ``owned=False`` so a walk sees what to grab next,
    in order. Only ``timeline`` universes get an order; a stale source lacking ``items`` yields none
    (caller falls back to per-media). De-duped by (media, id), first-wins. PURE."""
    owned_m = {t for t in (owned_movie_tmdbs or set()) if t is not None}
    owned_tv = set((owned_tvdb_to_sid or {}).keys())
    out: dict = {}
    for key, data in ((source or {}).get("universes") or {}).items():
        if not (isinstance(data, dict) and data.get("timeline")):
            continue
        seq: list = []
        seen: set = set()
        for it in data.get("items") or []:
            media = it.get("media")
            if media == "movie" and it.get("tmdb") is not None:
                ident, owned = ("movie", it["tmdb"]), it["tmdb"] in owned_m
            elif media == "show" and it.get("tvdb") is not None:
                ident, owned = ("show", it["tvdb"]), it["tvdb"] in owned_tv
            else:
                continue
            if (not owned and not include_unowned) or ident in seen:
                continue
            seen.add(ident)
            seq.append({"media": ident[0], "id": ident[1], "rank": len(seq), "owned": owned})
        if seq:
            out[key] = seq
    return out


def saga_member_engagement(unified_order, watched_movie_tmdbs, watched_show_tvdbs) -> dict:
    """Per-member CAUGHT-UP + overall-DEPTH engagement for the saga QUALITY credit (consumed by
    ``watch_likelihood.saga_credit``). The cross-media twin of ``universe_acquire_plan``, but for
    ELEVATING THE QUALITY of OWNED members rather than acquiring unowned ones.

    ``unified_order`` = :func:`unified_universe_order` (``include_unowned=True``):
    ``{key: [{media,id,rank,owned}…]}`` in saga order — so the priors of a member include UNOWNED and
    cross-media (movie+show) entries. ``watched_*`` = the HOUSEHOLD-watched ids keyed like the unified
    items (movie→tmdb, show→tvdb).

    Returns ``{(media, id): {"caught_up_frac": float, "saga_watched_frac": float, "saga": key}}``. A
    member in several sagas keeps the record of the saga it's most STRONGLY engaged in (highest
    ``max(caught_up, saga_watched)`` — which is exactly the engagement ``saga_credit`` keys off, so the
    resulting credit is identical to a cross-saga max; ``saga`` just names the saga that earned it, for
    the preview / GUI):
      * ``caught_up_frac`` — fraction of the member's timeline-PRIORS (lower rank) the household has
        watched (1.0 = you've seen everything before it → the frontier). 0.0 for a saga's FIRST entry
        (no priors) — the depth signal covers that case.
      * ``saga_watched_frac`` — fraction of the WHOLE saga the household has watched (same for every
        member of the saga).
      * ``saga`` — the universe key that earned the member its (winning) engagement.
    PURE — no I/O, deterministic."""
    wm = {t for t in (watched_movie_tmdbs or set()) if t is not None}
    ws = {t for t in (watched_show_tvdbs or set()) if t is not None}

    def _is_watched(media, mid):
        return (media == "movie" and mid in wm) or (media == "show" and mid in ws)

    out: dict = {}
    for key, members in (unified_order or {}).items():
        ordered = sorted(members, key=lambda m: m.get("rank", 0))
        total = len(ordered)
        n_watched = sum(1 for m in ordered if _is_watched(m["media"], m["id"]))
        saga_frac = round((n_watched / total) if total else 0.0, 4)
        priors_watched = 0
        for i, m in enumerate(ordered):
            caught = round((priors_watched / i) if i > 0 else 0.0, 4)
            ident = (m["media"], m["id"])
            strength = max(caught, saga_frac)
            cur = out.get(ident)
            if cur is None or strength > cur["_strength"]:    # keep the saga it's most engaged in
                out[ident] = {"caught_up_frac": caught, "saga_watched_frac": saga_frac,
                              "saga": key, "_strength": strength}
            if _is_watched(m["media"], m["id"]):
                priors_watched += 1
    for rec in out.values():
        rec.pop("_strength", None)
    return out
