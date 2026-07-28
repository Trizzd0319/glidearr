"""
machine_learning/next_watch — pure next-watch propensity over cached Plex signals.
================================================================================
The thin consumer sequenced in P1 so the flagship watchlist signal is not inert /
unvalidatable (DESIGN §5.1, Q6). It THINKS only: pure ``dict`` in, ``dict`` out over
the ALREADY-FETCHED ``plex/watchlist/union`` (and, later, ``plex/on_deck/union``).
The PlexWatchlistManager (a service) does the I/O; this layer never touches HTTP /
the service layer / the cache — enforced by the brain-purity guard, which now lists
``next_watch`` in ``_GUARDED_SUBPACKAGES``.

The deterministic A–G scorecard stays the curation authority; this only RANKS the
forward intent feed (the union ∪ Trakt ∪ MAL) for the future next-watch ranker.

Public API
----------
  * watchlist_intent(union, weights=None)      → {primary_id: {...}}   0-100 ranking feed
  * rank_next_watch(union, owned_ids, …)       → ordered candidate list
  * build_intent_index(…)                      → {tmdb: entry} / {tvdb: entry}, the
                                                 SCORER-facing index consumed by Group-A5
                                                 (scoring/_shared.watchlist_intent_score)
                                                 and by the watchlist delete shield.

``build_intent_index`` is why this subpackage is no longer orphaned: the Radarr and
Sonarr score passes build one per run and hand each title's entry to the scorer, so the
same forward-intent feed that ranks next-watch also carries A5.
"""
from __future__ import annotations


# ── source strength ladder ────────────────────────────────────────────────────
# THE SAME RANKING as ``services/acquisition/scorer._SOURCE_SCORE``, divided by 100 so it
# reads as a multiplier. Deliberately NOT a second opinion: the acquisition scorer already
# reasoned about which feeds mean "the household literally said watch this" (100) vs "an
# algorithm suggested it" (65) vs "it is merely airing" (55), and a title should not be
# graded one way when we decide to ACQUIRE it and another way when we decide to KEEP it.
# Any feed missing from this table contributes nothing (0.0) rather than a guessed tier.
INTENT_SOURCE_STRENGTH = {
    "plex_watchlist":        1.00,
    "trakt_watchlist":       1.00,
    "mal_plantowatch":       1.00,
    "trakt_recommendations": 0.65,
    "mal_suggestions":       0.65,
    "plex_playlist":         0.60,
    "people_cooccurrence":   0.60,
    "plex_hubs":             0.58,
    "mal_seasonal":          0.55,
}

#: Feeds that carry a REAL per-item listing timestamp. Everything else is UNDATED and is
#: scored without decay — see the ``listed_at`` note on :func:`build_intent_index`.
DATED_SOURCES = frozenset({"trakt_watchlist", "mal_plantowatch"})

# ── member ladder ─────────────────────────────────────────────────────────────
# Shared shape with :func:`watchlist_intent` (base 60, +12 per extra member, cap 100),
# expressed as a FRACTION of whatever cap the consumer applies. One household-intent
# opinion, two consumers: the next-watch ranker scales it to 0-100, Group-A5 scales it to
# ``scoring.watchlist_intent.cap``. A solo watchlister is deliberately worth 0.60 of the
# cap rather than the whole thing — otherwise the member term is invisible (every title
# already at cap) on the 258-of-259 solo distribution this household has today, and the
# signal could never grade a title two people both asked for.
MEMBER_BASE_FRACTION = 0.60
MEMBER_STEP_FRACTION = 0.12


def _primary_id(item: dict) -> str | None:
    ids = item.get("ids", {}) or {}
    primary = ids.get("tmdb") or ids.get("tvdb") or ids.get("imdb") or item.get("title")
    return str(primary) if primary else None


def member_fraction(members: int) -> float:
    """``members`` distinct household watchlisters → the 0..1 member multiplier.

    1 → 0.60, 2 → 0.72, 3 → 0.84, 4 → 0.96, 5+ → 1.0 (capped). Mirrors
    ``watchlist_intent``'s 60/+12/100 ladder exactly, one scale off."""
    n = max(1, int(members or 1))
    return min(1.0, MEMBER_BASE_FRACTION + MEMBER_STEP_FRACTION * (n - 1))


def watchlist_intent(union, weights: dict | None = None) -> dict:
    """Map each union title → a next-watch intent score in [0, 100].

    Intent rises with the number of distinct household members who watchlisted a
    title (explicit forward intent is the top-weighted next-watch feature). Pure:
    no I/O, deterministic, order-independent.

        {primary_id: {"intent": float, "watchlisted_by": [...], "title": str,
                      "type": str, "ids": {...}}}
    """
    w = {"base": 60.0, "per_extra_member": 12.0, "cap": 100.0}
    if weights:
        w.update({k: float(v) for k, v in weights.items() if k in w})

    out: dict = {}
    for item in (union or []):
        if not isinstance(item, dict):
            continue
        pid = _primary_id(item)
        if not pid:
            continue
        who = [m for m in (item.get("watchlisted_by") or []) if m]
        members = max(1, len(who))
        intent = min(w["cap"], w["base"] + (members - 1) * w["per_extra_member"])
        prev = out.get(pid)
        if prev and prev["intent"] >= intent:
            continue
        out[pid] = {
            "intent": round(intent, 2),
            "watchlisted_by": who,
            "title": item.get("title"),
            "type": item.get("type"),
            "ids": item.get("ids", {}) or {},
        }
    return out


def rank_next_watch(union, owned_ids=None, weights: dict | None = None) -> list:
    """Ordered next-watch candidate list (highest intent first). ``owned_ids`` (a set
    of primary-id strings) marks which titles the household already owns — an owned +
    unwatched watchlisted title ranks top of next-watch; not-owned feeds acquisition.
    Pure."""
    owned = {str(x) for x in (owned_ids or set())}
    scored = watchlist_intent(union, weights)
    rows = []
    for pid, v in scored.items():
        rows.append({**v, "primary_id": pid, "owned": pid in owned})
    rows.sort(key=lambda r: (r["owned"], r["intent"]), reverse=True)
    return rows


# ── scorer-facing intent index ────────────────────────────────────────────────

def _add(index: dict, key, *, source: str, member: str | None,
         listed_at: str | None, anchor: str | None) -> None:
    """Fold one feed observation into ``index[key]``. Idempotent and order-independent:
    the same title seen twice from the same member/source produces the same entry."""
    if key is None:
        return
    ent = index.get(key)
    if ent is None:
        ent = index[key] = {"sources": set(), "members": set(), "dated": {}, "anchors": set()}
    ent["sources"].add(source)
    if member:
        ent["members"].add(str(member))
    if listed_at:
        # Keep the NEWEST listing per source: re-adding a title to your Trakt watchlist is
        # fresh intent, and the oldest copy must not be allowed to speak for it.
        prev = ent["dated"].get(source)
        ent["dated"][source] = max(prev, str(listed_at)) if prev else str(listed_at)
    if anchor:
        ent["anchors"].add(str(anchor))


def _freeze(index: dict) -> dict:
    """Sets → sorted tuples so an entry is hashable-ish, JSON-safe and memo-stable."""
    out: dict = {}
    for key, ent in index.items():
        out[key] = {
            "sources": tuple(sorted(ent["sources"])),
            "members": tuple(sorted(ent["members"])),
            "dated": {k: ent["dated"][k] for k in sorted(ent["dated"])},
            # The shield's expiry anchor: the MOST RECENT activity among the members who
            # asked for this title. See scoring/_shared.intent_hold_active.
            "anchor": max(ent["anchors"]) if ent["anchors"] else None,
        }
    return out


def build_intent_index(*, plex_union=None, trakt_shows=None, trakt_movies=None,
                       mal_shows=None, mal_movies=None,
                       member_anchors=None, trakt_member=None) -> dict:
    """Fold every cached forward-intent feed into ``{"movies": {tmdb: entry},
    "shows": {tvdb: entry}}`` — the index Group-A5 and the delete shield read. PURE.

    entry = ``{"sources": (feed, …), "members": (member, …), "dated": {feed: iso},
               "anchor": iso | None}``

    ``plex_union``    ``plex/watchlist/union`` — the household union, each item carrying
                      ``ids{tmdb,tvdb}``, ``type`` and ``watchlisted_by[]``. **UNDATED**:
                      the union has no per-item timestamp, and the rolling
                      ``plex/watchlist/snapshot/`` files retain well under a day (8 files
                      spanning ~7 hours on this install), so a "first seen" derived from
                      them would say every title was added yesterday — a FABRICATED
                      timestamp, and a decay applied to it would be noise dressed as
                      evidence. Plex intent is therefore scored at full strength with no
                      decay until Plex exposes a real ``addedAt`` on watchlist items.
    ``trakt_shows`` / ``trakt_movies``
                      ``trakt/{user}/watchlist/{shows,movies}`` — Trakt DOES carry a real
                      per-item ``listed_at`` (this household's reaches back to 2020), so
                      these entries are DATED and decay.
    ``member_anchors`` ``{member: last_activity_iso}`` — each household member's most
                      recent play. The shield's expiry anchor, mirroring
                      ``coordinator/saga_retention_producer._attach_watchlist``: intent
                      lives while the member who expressed it is still active.
    ``mal_shows`` / ``mal_movies``
                      MAL ``plan_to_watch``, ALREADY BRIDGED to a library id by
                      ``services/mal/id_bridge`` — rows shaped
                      ``{"ids": {"tvdb"|"tmdb": int}, "updated_at": iso}``. MAL itself
                      carries no id that joins a Radarr/Sonarr row (``MALManager._norm``
                      emits ``ids {"tvdb": None, "tmdb": None, "mal": <id>}``), so the
                      join is an exact normalized-TITLE match done in the service layer
                      and cached under ``mal/{user}/id_map``; this function stays PURE and
                      receives only the resolved rows. **DATED**: MAL's
                      ``list_status.updated_at`` is a real per-item timestamp, so these
                      decay (``DATED_SOURCES``) exactly like Trakt's ``listed_at``.
    ``trakt_member``  the household member the Trakt account belongs to (``trakt.username``
                      resolved to a Tautulli/Plex name), so "Trizzd watchlisted it on Plex
                      AND Trakt" counts as ONE member, not two. MAL is attributed to the
                      SAME member for the same reason — one human keeping three lists is
                      one person asking, and counting them separately would walk a solo
                      title up the member ladder (0.60 → 0.84 of the cap) on nothing but
                      account sprawl."""
    anchors = {str(k): v for k, v in (member_anchors or {}).items() if v}
    movies: dict = {}
    shows: dict = {}

    for item in (plex_union or []):
        if not isinstance(item, dict):
            continue
        ids = item.get("ids") or {}
        who = [str(m) for m in (item.get("watchlisted_by") or []) if m] or [None]
        source = str(item.get("source") or "plex_watchlist")
        kind = str(item.get("type") or "").lower()
        bucket, raw_id = (shows, ids.get("tvdb")) if kind == "show" else (movies, ids.get("tmdb"))
        key = _int_or_none(raw_id)
        for member in who:
            _add(bucket, key, source=source, member=member, listed_at=None,
                 anchor=anchors.get(str(member)) if member else None)

    _anchor_trakt = anchors.get(str(trakt_member)) if trakt_member else None
    for raw in (trakt_shows or []):
        _add_trakt(shows, raw, "show", "tvdb", trakt_member, _anchor_trakt)
    for raw in (trakt_movies or []):
        _add_trakt(movies, raw, "movie", "tmdb", trakt_member, _anchor_trakt)

    # MAL rides the Trakt member/anchor deliberately — see ``trakt_member`` above.
    for raw in (mal_shows or []):
        _add_mal(shows, raw, "tvdb", trakt_member, _anchor_trakt)
    for raw in (mal_movies or []):
        _add_mal(movies, raw, "tmdb", trakt_member, _anchor_trakt)

    return {"movies": _freeze(movies), "shows": _freeze(shows)}


def _add_trakt(bucket: dict, raw, node_key: str, id_key: str, member, anchor) -> None:
    """One Trakt watchlist row (``{type, show|movie:{ids}, listed_at, rank}``) → the index."""
    if not isinstance(raw, dict):
        return
    node = raw.get(node_key) or raw.get(raw.get("type") or "") or {}
    key = _int_or_none((node.get("ids") or {}).get(id_key))
    _add(bucket, key, source="trakt_watchlist", member=member,
         listed_at=raw.get("listed_at"), anchor=anchor)


def _add_mal(bucket: dict, raw, id_key: str, member, anchor) -> None:
    """One BRIDGED MAL plan-to-watch row (``{ids:{tvdb|tmdb}, updated_at}``) → the index.

    ``updated_at`` is MAL's ``list_status.updated_at`` — the moment the entry last moved on
    the user's list, and the only real timestamp MAL offers. It is passed as ``listed_at``
    so ``mal_plantowatch`` decays through the same ``DATED_SOURCES`` path Trakt uses; a
    plan-to-watch entry last touched in 2023 is honest evidence of *stale* intent, not of
    none, so it floors at ``INTENT_STALE_FLOOR`` rather than vanishing."""
    if not isinstance(raw, dict):
        return
    key = _int_or_none((raw.get("ids") or {}).get(id_key))
    _add(bucket, key, source="mal_plantowatch", member=member,
         listed_at=raw.get("updated_at"), anchor=anchor)


def _int_or_none(v):
    try:
        return int(v) if v is not None and str(v).strip() != "" else None
    except (TypeError, ValueError):
        return None
