"""plex/playlists/identity.py — a Tautulli play -> the stable id the ledger keys on.

ONE implementation, shared by every consumer of the recommendation ledger. Two
already existed before this module: `discovery`'s caller-supplied resolver and
`affinity_builder._play_entity_resolver`, each inverting the owned inventories by
hand. Both were wrong in the same way, and a third copy would have been wrong in
a new way - which is how the collection-title mismatch (`GLD-PLY-18`) happened.

THE CROSSWALK IS THE PRIMARY SOURCE, and the reason is measurable.

`PlexPlaylistBuilderManager._refresh_rk_crosswalk` maintains
``tautulli/rating_key_crosswalk.json`` - ``ratingKey -> {tmdb|tvdb, title, year,
first_seen, last_seen}`` - which ACCUMULATES. It currently holds 1036 films
against 959 in the live movie inventory: 77 mappings for ratingKeys Plex has
since retired.

Inverting the owned inventory instead only ever knows the CURRENT ratingKey, so
a historical play pointing at a retired key resolves to nothing. Measured on the
real data: **185 of 979 plays resolved** by inventory inversion alone. 81% of the
household's viewing was being silently discarded before any matching logic ran -
not mismatched, simply invisible.

That is the exact failure the crosswalk was built to absorb, and it had been
sitting in the cache unused by any of this.

RESOLUTION ORDER, most durable first:

    1. crosswalk movies   ratingKey -> tmdb            (survives re-scans)
    2. crosswalk shows    grandparentRatingKey -> tvdb (SHOW level)
    3. episode inventory  ratingKey -> tvdb:s:e        (EPISODE level, current only)
    4. movie inventory    ratingKey -> tmdb            (current only)

EPISODE AND SHOW ARE BOTH RETURNED, not one or the other. Surfaces record at the
level they RECOMMEND at: Anniversary surfaces SHOWS because a show has an
anniversary, while Tonight surfaces a specific next EPISODE. A play has to be
able to meet either, so this returns every identity it can establish and lets
`discovery.play_keys` decide which placements those satisfy.

NEVER GUESSES. An unresolvable play returns an empty list and is counted by
nothing - it is not evidence of a miss, it is an absence of evidence (P-C).
"""
from __future__ import annotations

import json

_CROSSWALK = ("tautulli", "rating_key_crosswalk.json")


def _unwrap(blob):
    """Cache files are written bare or inside a ``{"value": ...}`` envelope."""
    if isinstance(blob, dict) and "value" in blob and "movies" not in blob:
        return blob["value"]
    return blob


def load_crosswalk(base_dir):
    """``(movies, shows)`` from the ratingKey crosswalk. ``({}, {})`` on any miss.

    Missing is normal on a fresh install - the crosswalk is built by the playlist
    builder's first run - and it degrades to inventory-only resolution rather
    than failing, which is exactly today's behaviour.
    """
    if base_dir is None:
        return {}, {}
    try:
        from pathlib import Path
        path = Path(base_dir).joinpath(*_CROSSWALK)
        data = _unwrap(json.loads(path.read_text(encoding="utf-8"))) or {}
    except (OSError, ValueError, TypeError):
        return {}, {}
    movies = data.get("movies") if isinstance(data, dict) else None
    shows = data.get("shows") if isinstance(data, dict) else None
    return (movies if isinstance(movies, dict) else {},
            shows if isinstance(shows, dict) else {})


def build_resolver(*, base_dir=None, movie_inventory=None, episode_inventory=None):
    """``play -> [entity_id, ...]``, most specific first.

    Returns a LIST because one play can legitimately satisfy placements recorded
    at different depths - an episode of a show whose anniversary was surfaced
    answers both ``tvdb:<series>:<s>:<e>`` and ``tvdb:<series>``.

    Read-time inversion is acceptable here in a way it was NOT at write time
    (`GLD-PLY-24`): a key that fails to resolve yields an unmatched play, which
    is an undercount visible as a lower completion rate. The same lookup while
    RECORDING would have written a different title that inherited the number,
    which nothing downstream could detect. Fail toward missing, never toward
    wrong.
    """
    xw_movies, xw_shows = load_crosswalk(base_dir)

    inv_tmdb = {str(r["rating_key"]): str(t)
                for t, r in (movie_inventory or {}).items()
                if isinstance(r, dict) and r.get("rating_key") is not None}
    inv_join = {str(r["rating_key"]): str(k)
                for k, r in (episode_inventory or {}).items()
                if isinstance(r, dict) and r.get("rating_key") is not None}
    inv_show = {}
    for k, r in (episode_inventory or {}).items():
        if isinstance(r, dict) and r.get("grandparent_rating_key") is not None:
            series = str(k).split(":")[0]
            if series:
                inv_show.setdefault(str(r["grandparent_rating_key"]), series)

    def _resolve(row):
        if not isinstance(row, dict):
            return []
        out, seen = [], set()

        def add(eid):
            if eid and eid not in seen:
                seen.add(eid)
                out.append(eid)

        rk = str(row.get("rating_key") or "")
        grk = str(row.get("grandparent_rating_key") or "")

        # EPISODE identity first: the most specific thing a play can claim.
        if rk in inv_join:
            j = inv_join[rk]
            add(j if j.startswith("tvdb:") else f"tvdb:{j}")

        # SHOW identity, crosswalk before inventory - the crosswalk survives a
        # re-scan that retired the series' ratingKey.
        if grk:
            ent = xw_shows.get(grk)
            tvdb = (ent or {}).get("tvdb") if isinstance(ent, dict) else None
            if tvdb not in (None, ""):
                add(f"tvdb:{tvdb}")
            elif grk in inv_show:
                add(f"tvdb:{inv_show[grk]}")

        # MOVIE identity, crosswalk before inventory, same reasoning.
        if rk:
            ent = xw_movies.get(rk)
            tmdb = (ent or {}).get("tmdb") if isinstance(ent, dict) else None
            if tmdb not in (None, ""):
                add(str(tmdb))
            elif rk in inv_tmdb:
                add(inv_tmdb[rk])

        # A SHOW play (not an episode) carries its own ratingKey in the show map.
        if rk and not grk:
            ent = xw_shows.get(rk)
            tvdb = (ent or {}).get("tvdb") if isinstance(ent, dict) else None
            if tvdb not in (None, ""):
                add(f"tvdb:{tvdb}")

        return out

    return _resolve


def first_of(resolver):
    """Adapt a multi-identity resolver to the single-value contract.

    ``discovery.play_keys`` takes ``entity_of_play(row) -> one id``. Passing this
    keeps that signature working while the resolver stays multi-valued for
    callers that want every identity.
    """
    def _first(row):
        ids = resolver(row)
        return ids[0] if ids else None
    return _first
