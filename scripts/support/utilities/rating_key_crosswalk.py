"""rating_key_crosswalk.py — a durable Plex ratingKey → native-id map.

WHY THIS EXISTS. Plex re-issues a ratingKey whenever an item is re-scanned, re-matched or
duplicated, and Tautulli records the key AS IT WAS AT PLAY TIME. Nothing in Plex or
Tautulli remembers the old value, so a play from before a re-scan can no longer be joined
to the title it belongs to. Measured on a real profile: of 178 finished movie plays only
19 ratingKeys were still valid — 11%. The other 150 pointed at titles the household
demonstrably still owns.

The existing workaround is to match on ``(title, year)`` / ``(series, season, episode)``
instead, and that recovers most of them. But it fails exactly where two systems spell a
title differently — "Black Panther" resolved as *missing* on a library that owns it — and
it can never be better than the agreement between Plex, Tautulli and *arr naming.

So: an APPEND-ONLY crosswalk. Every run resolves the ratingKeys that are valid RIGHT NOW
and merges them in; nothing is ever evicted. A key retired by a re-scan keeps pointing at
the right title forever, and coverage compounds run over run instead of decaying.

SCOPE. Movies (``rating_key`` → tmdb) and shows (``grandparent_rating_key`` → tvdb).
Episodes are deliberately NOT stored: their ``(series, season, episode)`` identity already
resolves 117/117 where ratingKey managed 11/117, so an episode-level map would add ~15k
entries per run of permanent growth for a problem that is already solved.

PURE — no I/O, no manager, no config. The caller supplies the inventories and persists the
result.
"""
from __future__ import annotations

# Entries carry the run that first and last SAW the key valid. `first_seen` dates the
# mapping; `last_seen` shows whether Plex still issues it, which is what makes a stale
# entry recognisable without deleting it.
_MOVIES = "movies"
_SHOWS = "shows"


def _coerce_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def empty_crosswalk() -> dict:
    """The canonical empty shape, so a first run and a loaded one look identical."""
    return {_MOVIES: {}, _SHOWS: {}, "generated_at": None}


def _merge(bucket: dict, key: str, payload: dict, now) -> str:
    """Insert or refresh one entry. Returns 'new' | 'refreshed' | 'conflict'.

    A CONFLICT is a ratingKey that used to mean one title and now means another — Plex
    recycling a retired key onto a different item. The NEWEST wins (it is what Plex means
    today) but the previous id is kept in ``was`` so a play recorded under the old meaning
    is still interpretable rather than silently mis-attributed.
    """
    cur = bucket.get(key)
    if cur is None:
        payload = dict(payload)
        payload["first_seen"] = now
        payload["last_seen"] = now
        bucket[key] = payload
        return "new"
    _id_field = "tmdb" if "tmdb" in payload else "tvdb"
    if cur.get(_id_field) != payload.get(_id_field):
        was = list(cur.get("was") or [])
        was.append({_id_field: cur.get(_id_field), "title": cur.get("title"),
                    "until": cur.get("last_seen")})
        cur.update(payload)
        cur["was"] = was[-5:]          # bounded: a key recycled 5+ times is noise
        cur["last_seen"] = now
        return "conflict"
    cur["last_seen"] = now
    if payload.get("title"):
        cur["title"] = payload["title"]
    return "refreshed"


def build_crosswalk(prior=None, *, movie_inventory=None, episode_inventory=None,
                    now=None) -> tuple:
    """``(crosswalk, stats)`` — ``prior`` merged with everything resolvable today.

    ``movie_inventory``   ``{tmdb: {rating_key, title, year}}`` (plex/movies/owned_inventory)
    ``episode_inventory`` ``{"tvdb:season:episode": {rating_key, grandparent_rating_key,
                          series_title, ...}}`` (plex/episodes/owned_inventory) — the SHOW
                          id is the key's first segment, so no extra lookup is needed.

    Never evicts. A ratingKey Plex no longer issues simply stops being refreshed; it stays
    resolvable, which is the entire point.
    """
    out = dict(prior or empty_crosswalk())
    out.setdefault(_MOVIES, {})
    out.setdefault(_SHOWS, {})
    stats = {"movies_new": 0, "movies_refreshed": 0, "movies_conflict": 0,
             "shows_new": 0, "shows_refreshed": 0, "shows_conflict": 0}

    for tmdb, v in (movie_inventory or {}).items():
        if not isinstance(v, dict):
            continue
        rk = v.get("rating_key")
        tm = _coerce_int(tmdb)
        if rk is None or tm is None:
            continue
        r = _merge(out[_MOVIES], str(rk),
                   {"tmdb": tm, "title": v.get("title"), "year": v.get("year")}, now)
        stats[f"movies_{r}"] += 1

    seen_shows: set = set()
    for k, v in (episode_inventory or {}).items():
        if not isinstance(v, dict):
            continue
        grk = v.get("grandparent_rating_key")
        if grk is None:
            continue
        # "389250:1:1" → the show's tvdb is the leading segment.
        tv = _coerce_int(str(k).split(":", 1)[0])
        if tv is None:
            continue
        pair = (str(grk), tv)
        if pair in seen_shows:              # one entry per show, not per episode
            continue
        seen_shows.add(pair)
        r = _merge(out[_SHOWS], str(grk),
                   {"tvdb": tv, "title": v.get("series_title")}, now)
        stats[f"shows_{r}"] += 1

    out["generated_at"] = now
    stats["movies_total"] = len(out[_MOVIES])
    stats["shows_total"] = len(out[_SHOWS])
    return out, stats


def seed_from_history(crosswalk, history, by_title, *, now=None, min_pct=85.0,
                      norm=None) -> dict:
    """Capture a ratingKey → tmdb mapping for plays whose key is ALREADY stale.

    The crosswalk built from a live inventory can only ever learn keys Plex still issues,
    so on a first run it recovers nothing that has already churned — 150 of one profile's
    178 finished plays. This closes that gap ONCE: for each finished play, resolve its
    TITLE to a tmdb and record the mapping under the play's own (stale) ratingKey.

    After that the mapping is permanent and title-independent. It is the one moment title
    matching is used to bootstrap something more durable than itself — including for titles
    that later drift apart between Plex and *arr, which is precisely where title matching
    fails ("Black Panther" reading as absent on a library that owns it).

    ``norm`` is the title normaliser to match ``by_title``'s keys — pass the caller's own so
    the two cannot disagree. Returns stats.
    """
    _n = norm or (lambda v: str(v or "").strip().lower())
    bucket = (crosswalk or {}).setdefault(_MOVIES, {})
    added = 0
    for row in history or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("media_type", "")).lower() != "movie":
            continue
        try:
            if float(row.get("percent_complete") or 0) < min_pct:
                continue
        except (TypeError, ValueError):
            continue
        rk = row.get("rating_key")
        if rk is None or str(rk) in bucket:
            continue                       # never overwrite a live-resolved entry
        tm = (by_title or {}).get(_n(row.get("title")))
        if tm is None:
            continue
        bucket[str(rk)] = {"tmdb": int(tm), "title": row.get("title"),
                           "first_seen": now, "last_seen": now, "via": "title-seed"}
        added += 1
    return {"seeded": added, "movies_total": len(bucket)}


def resolve_movie_tmdb(rating_key, crosswalk) -> "int | None":
    """A play's ratingKey → tmdb, including keys Plex has long since retired."""
    e = ((crosswalk or {}).get(_MOVIES) or {}).get(str(rating_key))
    return _coerce_int((e or {}).get("tmdb"))


def resolve_show_tvdb(rating_key, crosswalk) -> "int | None":
    """An episode play's ``grandparent_rating_key`` → the SHOW's tvdb."""
    e = ((crosswalk or {}).get(_SHOWS) or {}).get(str(rating_key))
    return _coerce_int((e or {}).get("tvdb"))


def movie_rk_map(crosswalk) -> dict:
    """``{rating_key: tmdb}`` — the whole movie side, for a bulk join."""
    return {rk: t for rk, e in ((crosswalk or {}).get(_MOVIES) or {}).items()
            if (t := _coerce_int((e or {}).get("tmdb"))) is not None}


def show_rk_map(crosswalk) -> dict:
    """``{grandparent_rating_key: tvdb}`` — the whole show side, for a bulk join."""
    return {rk: t for rk, e in ((crosswalk or {}).get(_SHOWS) or {}).items()
            if (t := _coerce_int((e or {}).get("tvdb"))) is not None}
