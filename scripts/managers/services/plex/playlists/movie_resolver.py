"""
plex/playlists/movie_resolver.py — join owned movies → Plex ratingKeys → a movie plan.
================================================================================
The movie twin of ``tv_resolver``. Pure given its inputs (no I/O — the builder loads the
caches/parquet and passes scalars), so it is fully unit-testable:

  owned movies (radarr ``movie_files.parquet`` rows, carrying ``tmdb_id`` + collection /
      universe tags + release dates + a household watchability score)
    ⋈ plex/movies/owned_inventory   (tmdb → Plex movie ratingKey)
    ⋈ per-user watched movies        (Tautulli per-user history)
    ⋈ per-movie watchability score
  → PlaylistInput(medium="movie", grouped by collection/universe, timed by release date)
  → order_items (the brain — groups, drops watched, ranks, caps)
  → PlaylistPlan (+ resolution stats).

Movies need NO expansion (a movie is a single item, unlike a show→episodes tree), so this
is simpler than the TV resolver: build inputs, hand them straight to order_items.
"""
from __future__ import annotations

from scripts.managers.machine_learning.playlists.models import PlaylistInput
from scripts.managers.machine_learning.playlists.ordering import order_items


def _norm(s) -> str:
    # strip surrounding quotes too — movie_files titles are occasionally quote-wrapped
    return str(s or "").strip().strip('"').strip().lower()


def _coerce_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _coll_key(v):
    """Clean a collection/universe GROUPING key. ``movie_files`` numeric columns round-trip
    a missing value as float ``NaN`` (``to_numeric(errors='coerce')``), and ``NaN`` is
    TRUTHY in Python — so without this guard every collection-less movie would fuse under
    the literal key ``'nan'`` into one giant bogus group. NaN/empty → None; a whole-number
    float id → an int string (``500.0`` → ``'500'``) so the key is stable."""
    if v is None or (isinstance(v, float) and v != v):        # None or NaN
        return None
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    s = str(v).strip()
    return s if s and s.lower() != "nan" else None


def _release_date(mv: dict):
    """Theatrical-priority ISO date for chronological within-group ordering."""
    for k in ("in_cinemas_date", "digital_release_date", "physical_release_date"):
        v = mv.get(k)
        if v is not None:
            s = str(v).strip()
            if s and s.lower() not in ("nat", "none", "nan"):
                return s[:10]
    return None


def watched_movie_keys(history: list, *, min_pct: float = 85.0) -> set:
    """Per-user finished-MOVIE identities from Tautulli history (``percent_complete >=
    min_pct``). Mixes the episode-style identities so the join survives Plex ratingKey
    churn: the ratingKey (``str``) when fresh, plus a ``(title, year)`` tuple that
    survives a re-scan/duplicate — the movie analog of watched_episode_keys."""
    out: set = set()
    for row in history or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("media_type", "")).lower() != "movie":
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
        t, y = _norm(row.get("title")), _coerce_int(row.get("year"))
        if t and y is not None:
            out.add((t, y))
    return out


def movie_inputs(owned_movies: list, owned_inventory: dict, watched, movie_scores: dict):
    """Resolve owned movies (keyed by tmdb) to Plex ratingKeys + build grouped/timed
    ``PlaylistInput``s — the candidates WITHOUT ordering. Shared by :func:`build_movie_plan`
    and the combined cross-medium plan. Returns ``(list[PlaylistInput], stats)``.

    ``watched`` is the mixed identity set from :func:`watched_movie_keys` (ratingKeys +
    ``(title, year)`` tuples); a movie is watched if EITHER hits. ``movie_scores`` is keyed
    by tmdb (the per-user ranking score the builder computed)."""
    watched = set(watched or ())
    inv = owned_inventory or {}
    scores = movie_scores or {}
    stats = {"owned": len(owned_movies or []), "resolved": 0, "unresolved": 0}

    inputs: list = []
    for mv in owned_movies or []:
        tmdb = _coerce_int(mv.get("tmdb_id"))
        match = inv.get(str(tmdb)) if tmdb is not None else None
        rk = str(match["rating_key"]) if (match and match.get("rating_key")) else None
        if rk is None:
            stats["unresolved"] += 1          # owned but not matchable on this Plex server
            continue
        stats["resolved"] += 1
        title, year = _norm(mv.get("title")), _coerce_int(mv.get("year"))
        is_watched = (rk in watched) or (bool(title) and year is not None and (title, year) in watched)
        franchise = _coll_key(mv.get("collection_tmdb_id")) or _coll_key(mv.get("collection_name"))
        uni = _coll_key(mv.get("universe_name"))
        inputs.append(PlaylistInput(
            rating_key=rk, medium="movie",
            title=(mv.get("title") or (match.get("title") or "")),
            score=scores.get(tmdb),
            watched=is_watched,
            franchise=franchise,
            universes=((uni.lower(),) if uni else ()),
            release_date=_release_date(mv),
            year=year,
            cert=mv.get("certification"),
        ))

    stats["movies"] = len(inputs)
    return inputs, stats


def build_movie_plan(owned_movies: list, owned_inventory: dict, watched, movie_scores: dict,
                     *, family: str = "up_next", max_items: int = 100):
    """Build movie candidates (:func:`movie_inputs`) then hand them to the brain to order.
    Returns ``(PlaylistPlan, stats)``."""
    inputs, stats = movie_inputs(owned_movies, owned_inventory, watched, movie_scores)
    plan = order_items(inputs, family=family, max_items=max_items)
    stats["in_plan"] = len(plan.items)
    return plan, stats
