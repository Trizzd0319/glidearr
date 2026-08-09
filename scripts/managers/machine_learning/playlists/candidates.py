"""candidates.py — for each ranked group, the ONE thing to offer.

`habits.py` answers "which groups belong to Tuesday". This answers "and what,
exactly, do I put in the list for each of them" - and the answer differs by
group level, because the levels are not the same kind of thing:

    series:47889      -> the NEXT UNWATCHED episode, in (season, episode) order
    franchise:mcu     -> the next unwatched film in the saga's curated order
    genre:horror      -> the highest-watchability unwatched film of that genre
    medium:movie      -> the highest-watchability unwatched film, full stop

SPOILER SAFETY IS STRUCTURAL HERE, not a check bolted on. An episode candidate
is the earliest unwatched by ``(season, episode)`` - never by air date, which is
region-dependent and wrong for anime and re-releases. That ordering makes it
impossible to offer S03E07 while S03E06 is unseen, regardless of metadata
quality, which is the same guarantee `ordering.order_within_group` provides.
Specials (season 0) are excluded, as they are there.

THE OVERLAP TRAP. The levels are not disjoint: an MCU film is a member of
``franchise:mcu`` AND ``genre:action`` AND ``medium:movie``. Picking one item per
group independently would put the same film in the list two or three times. So
selection is a single pass in RANK ORDER with a claimed-set - the strongest
group picks first and later groups skip what is taken. That is why
:func:`pick_one_per_group` exists here rather than the caller reusing
``habits.one_per_show``, which has no such protection and should be retired in
favour of this.

PURE. No I/O, no manager, no config. Inventories and predicates in, keys out.
"""
from __future__ import annotations

#: Season number treated as "specials". Excluded from episode candidates for the
#: same reason ordering.py excludes them: a special carries no "must precede"
#: relationship, so it can neither be spoiled nor be the next thing to watch.
SPECIALS_SEASON = 0


# ── watched predicates ───────────────────────────────────────────────────────
def watched_by_rating_key(watched):
    """The simple predicate: a row is watched if its ratingKey is in ``watched``.

    Deliberately a CALLABLE rather than this module reaching into a set of a
    known shape. The repo's real watched set mixes three identity kinds
    (ratingKey, ``(series, season, episode)``, ``(series, title)``) because a
    Plex re-scan retires ratingKeys and a play recorded before it would
    otherwise stop matching - measured at 11/117 on one show. Reproducing that
    logic here would be a second copy of it; the caller passes theirs in.
    """
    seen = {str(w) for w in (watched or ())}

    def _is_watched(join_key, row):
        rk = (row or {}).get("rating_key")
        return rk is not None and str(rk) in seen

    return _is_watched


def _episode_sort_key(join_key):
    """``(season, episode)`` from a ``tvdb:s:e`` inventory key, or None.

    None for a malformed key, and the caller drops it: an episode whose position
    cannot be established must never be offered, because "next" is meaningless
    without it and guessing risks a spoiler.
    """
    parts = str(join_key).split(":")
    if len(parts) < 3:
        return None
    try:
        return int(parts[-2]), int(parts[-1])
    except (TypeError, ValueError):
        return None


# ── episodes ─────────────────────────────────────────────────────────────────
def episode_candidates(inventory, *, is_watched, groups=None,
                       include_specials: bool = False) -> dict:
    """``{"series:<grandparent_rk>": [join_key, ...]}`` in spoiler-safe order.

    ``inventory`` is ``plex/episodes/owned_inventory`` -
    ``{"tvdb:s:e": {rating_key, grandparent_rating_key, ...}}``. The group handle
    is built from ``grandparent_rating_key`` because that is what Tautulli
    history rows carry, so the habit key and the candidate key join directly.

    ``groups`` restricts the work to the series actually asked for; None does
    the whole inventory. Restricting matters at 14k episodes.
    """
    wanted = None if groups is None else {str(g) for g in groups}
    by_series: dict = {}
    for join_key, row in (inventory or {}).items():
        if not isinstance(row, dict):
            continue
        gp = row.get("grandparent_rating_key")
        if gp in (None, ""):
            continue
        group = f"series:{gp}"
        if wanted is not None and group not in wanted:
            continue
        pos = _episode_sort_key(join_key)
        if pos is None:
            continue
        if not include_specials and pos[0] == SPECIALS_SEASON:
            continue
        if is_watched(join_key, row):
            continue
        by_series.setdefault(group, []).append((pos, str(join_key)))

    return {g: [k for _pos, k in sorted(items)] for g, items in by_series.items()}


# ── movies ───────────────────────────────────────────────────────────────────
def movie_candidates(inventory, *, is_watched, franchise_by_id=None,
                     genres_by_id=None, score_by_id=None, order_by_id=None,
                     groups=None) -> dict:
    """``{"franchise:x" | "genre:y" | "medium:movie": [movie_id, ...]}``.

    One film feeds SEVERAL groups - its franchise, each of its genres, and the
    medium - which is exactly why :func:`pick_one_per_group` de-duplicates.

    ORDERING DIFFERS BY LEVEL, because the claim differs:
      * a FRANCHISE is a saga, so it orders by ``order_by_id`` (the curated
        universe order). A film with no order sorts after the ordered ones
        rather than being dropped - an unplaced sequel is still watchable.
      * a GENRE or the MEDIUM claims "highest watchability", so it orders by
        ``score_by_id`` and a film with NO SCORE is EXCLUDED. Offering an
        unranked film as the best of its genre would be a straightforward lie,
        and an absent score is not a low one (P-C).
    """
    wanted = None if groups is None else {str(g) for g in groups}
    scores = score_by_id or {}
    orders = order_by_id or {}
    fran = franchise_by_id or {}
    gens = genres_by_id or {}

    buckets: dict = {}

    def _add(group, mid, sort_key):
        if wanted is not None and group not in wanted:
            return
        buckets.setdefault(group, []).append((sort_key, str(mid)))

    for mid, row in (inventory or {}).items():
        if not isinstance(row, dict) or is_watched(mid, row):
            continue
        mid = str(mid)
        score = scores.get(mid)
        # franchise: curated order first, unplaced after, then id for stability
        f = fran.get(mid)
        if f:
            o = orders.get(mid)
            _add(f"franchise:{str(f).strip().lower()}",
                 mid, (0, o, mid) if o is not None else (1, 0, mid))
        # genre + medium: score-ranked, unscored excluded
        if score is not None:
            for g in (gens.get(mid) or ()):
                if g:
                    _add(f"genre:{str(g).strip().lower()}", mid, (-float(score), mid))
            _add("medium:movie", mid, (-float(score), mid))

    return {g: [m for _k, m in sorted(items)] for g, items in buckets.items()}


# ── selection ────────────────────────────────────────────────────────────────
def pick_one_per_group(ranked_groups, pools, *, limit=None, accept=None) -> list:
    """One item per group, in rank order, with NO item used twice.

    Returns ``[(group, item), ...]``. The de-duplication is the point: the
    levels overlap (an MCU film is in ``franchise:mcu``, ``genre:action`` and
    ``medium:movie``), so independent picks would repeat it. Walking in RANK
    order means the strongest group gets first refusal and weaker ones fall
    through to their next choice rather than being silently emptied.

    ``accept(item) -> bool`` filters candidates DURING the walk, not after it.
    That distinction matters: a runtime cap applied afterwards would just delete
    the group's entry, whereas rejecting here lets the group offer its NEXT
    candidate - so a Tuesday whose top pick is a two-hour film still gets that
    show's shorter episode instead of nothing.

    A group whose whole pool is rejected or already claimed contributes nothing
    and is skipped - it has nothing left to offer tonight, which is not an error.
    """
    claimed: set = set()
    out = []
    for group in (ranked_groups or ()):
        for item in (pools.get(str(group)) or ()):
            if item in claimed:
                continue
            if accept is not None and not accept(item):
                continue
            claimed.add(item)
            out.append((str(group), item))
            break
        if limit is not None and len(out) >= int(limit):
            break
    return out


def merge_pools(*pools) -> dict:
    """Combine candidate maps. Later maps do NOT overwrite earlier ones for a
    shared key; their items are appended, so a group that somehow appears in two
    sources keeps both in priority order."""
    out: dict = {}
    for pool in pools:
        for group, items in (pool or {}).items():
            out.setdefault(group, []).extend(items)
    return out
