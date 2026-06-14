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
"""
from __future__ import annotations


def _primary_id(item: dict) -> str | None:
    ids = item.get("ids", {}) or {}
    primary = ids.get("tmdb") or ids.get("tvdb") or ids.get("imdb") or item.get("title")
    return str(primary) if primary else None


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
