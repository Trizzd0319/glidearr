"""bucket_merge.py — flatten the enrich-daemon's per-id Trakt buckets into flat,
parquet-ready columns.
================================================================================
The systematic bridge from the daemon's scattered ``{id}.json.gz`` buckets
(people / summary / ratings / related, single-sourced in ``daemon_paths``) into the
COLUMNS the movie_files / episode_files parquets carry — so the watchability scorer
and the cross-medium (TV↔movie) affinity read one consistent column space instead
of opening thousands of gz files.

PURE: stdlib only (no IO, no service/_api imports). A cache/service manager reads the
gz buckets (via TraktShow/MovieCacheManager) and passes the decoded dicts here; the
returned column dict is what the manager broadcasts into the parquet. Column names
mirror ``radarr/cache/movie_files.py::_extract_people`` so movies and shows align.
"""
from __future__ import annotations

import json


def _pipe(names) -> str | None:
    """De-duped, order-preserving pipe-join — matches the movie_files people columns."""
    seen: list[str] = []
    for n in names:
        s = (str(n).strip() if n is not None else "")
        if s and s not in seen:
            seen.append(s)
    return "|".join(seen) or None


def flatten_trakt_people(people: dict, *, cast_limit: int = 10) -> dict:
    """Daemon people ``{cast:[{name,order}], crew:[{name,job,department}]}`` → flat
    pipe-separated columns (cast_names / director_names / producer_names /
    writer_names / composer_names), mirroring movie_files. Empty / missing → None."""
    people = people or {}
    cast = people.get("cast") or []
    crew = people.get("crew") or []
    cast_names = [c.get("name") for c in sorted(cast, key=lambda c: c.get("order", 9999))][:cast_limit]
    directors, producers, writers, composers = [], [], [], []
    for m in crew:
        name = m.get("name")
        if not name:
            continue
        job = m.get("job") or ""
        dept = (m.get("department") or "").lower()
        if job == "Director" or dept == "directing":
            directors.append(name)
        elif dept == "production" and "producer" in job.lower():
            producers.append(name)
        elif dept == "writing" or job in ("Screenplay", "Story", "Writer"):
            writers.append(name)
        elif job == "Original Music Composer":
            composers.append(name)
    return {
        "cast_names":     _pipe(cast_names),
        "director_names": _pipe(directors),
        "producer_names": _pipe(producers),
        "writer_names":   _pipe(writers),
        "composer_names": _pipe(composers),
    }


def genres_json(*sources) -> str | None:
    """First non-empty genre list among the sources → JSON string for the parquet
    ``genres`` column. Accepts a bare list (e.g. Sonarr series_obj['genres']) or a
    dict carrying a ``genres`` key (e.g. the daemon show summary). Order = priority."""
    for src in sources:
        g = src.get("genres") if isinstance(src, dict) else src
        if isinstance(g, list) and g:
            out = [str(x).strip() for x in g if x and str(x).strip()]
            if out:
                return json.dumps(out)
    return None


def trakt_rating_cols(ratings: dict) -> dict:
    """Daemon show/movie ratings ``{rating, votes, ...}`` → {trakt_rating, trakt_vote_count}."""
    ratings = ratings or {}
    val = ratings.get("rating")
    votes = ratings.get("votes")
    return {
        "trakt_rating":     float(val) if isinstance(val, (int, float)) else None,
        "trakt_vote_count": int(votes) if isinstance(votes, (int, float)) else None,
    }


def show_enrichment_columns(*, people: dict | None = None, ratings: dict | None = None,
                            summary: dict | None = None, sonarr_genres=None) -> dict:
    """One call → all the flat columns to broadcast onto a series' episode rows:
    cast/crew (daemon people), genres (Sonarr first, daemon summary fallback), and
    Trakt audience rating. Every value is parquet-safe (str/float/int/None)."""
    cols = flatten_trakt_people(people or {})
    cols["genres"] = genres_json(sonarr_genres, summary or {})
    cols.update(trakt_rating_cols(ratings or {}))
    return cols
