"""
scoring/critic.py — critic-consensus blend (pure).
================================================================================
Relocated from ``radarr/quality/space_pressure._row_critic_avg`` (ML Step 2).
Normalises each present critic source to 0-10 and averages them: imdb / tmdb at
their native 0-10 scale, trakt / rotten-tomatoes / metacritic scaled by 0.1
(they arrive on a 0-100 scale). Returns None when no source is available.

Pure — takes a mapping of rating values (a Parquet row, dict, or any object with
the rating keys); no DataFrame, no HTTP. The service extracts the columns and
calls this (the secondary rank key in space-pressure delete tiering).
"""
from __future__ import annotations

# (source column name, scale to bring it onto a 0-10 axis)
_CRITIC_SOURCES = (
    ("imdb_rating", 1.0),
    ("tmdb_rating", 1.0),
    ("trakt_rating", 0.1),
    ("rotten_tomatoes_score", 0.1),
    ("metacritic_score", 0.1),
)


def critic_avg(ratings) -> "float | None":
    """Mean of the present, positive critic sources on a 0-10 axis, or None.

    ``ratings`` is any mapping exposing ``.get`` (a dict or a pandas row) with any
    of imdb_rating / tmdb_rating / trakt_rating / rotten_tomatoes_score /
    metacritic_score. Missing / non-numeric / NaN / non-positive values are
    skipped — identical to the original per-row computation.
    """
    get = ratings.get if hasattr(ratings, "get") else (lambda k, d=None: ratings[k] if k in ratings else d)
    vals = []
    for col, scale in _CRITIC_SOURCES:
        v = get(col)
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv != fv:            # NaN guard
            continue
        if fv > 0:
            vals.append(fv * scale)
    return (sum(vals) / len(vals)) if vals else None
