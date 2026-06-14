"""
classification/franchise.py — franchise/universe membership (pure).
================================================================================
Relocated from ``radarr/cache/movie_files`` (ML Step 5c). Two pure resolvers:
which movies are the franchise "entry" (earliest year per collection) and which
``movie_file_id`` values are franchise-protected (never delete the franchise
anchor of a collection). PURE — no HTTP, no global_cache; the service keeps the
Parquet/movie-list read and delegates here.

Public API:
  * resolve_franchise_entries(movies) -> set[int]   (movie_ids; earliest-year-per-collection)
  * build_franchise_file_ids(df) -> frozenset       (protected movie_file_id set)
"""
from __future__ import annotations

from collections import defaultdict

import pandas as pd


def resolve_franchise_entries(movies: list[dict]) -> set[int]:
    """Return the set of movie_ids that are franchise entries (the earliest-year
    movie in each collection). Movies not in any collection are never entries."""
    collections: dict[str, list[dict]] = defaultdict(list)
    for movie in movies:
        coll = movie.get("collection") or {}
        coll_name = coll.get("name")
        if coll_name:
            collections[coll_name].append(movie)

    franchise_ids: set[int] = set()
    for coll_movies in collections.values():
        # Earliest year wins
        valid = [m for m in coll_movies if m.get("year")]
        if not valid:
            valid = coll_movies
        earliest = min(valid, key=lambda m: m.get("year", 9999))
        mid = earliest.get("id")
        if mid is not None:
            franchise_ids.add(mid)

    return franchise_ids


def build_franchise_file_ids(df) -> "frozenset":
    """Return the frozenset of ``movie_file_id`` values that must NEVER be deleted.

    Two categories of protection:
      1. Real franchise entries: ``is_franchise_entry`` True AND ``movie_file_id`` not NaN.
      2. De-facto franchise: for collections with no resolved franchise entry,
         the earliest-year WATCHED movie's file_id.
    """
    franchise_file_ids: set = set()

    if "is_franchise_entry" not in df.columns or "movie_file_id" not in df.columns:
        return frozenset()

    _fe_mask = (
        df["is_franchise_entry"].infer_objects(copy=False).fillna(False).astype(bool)
    )

    # 1. Real franchise entry file IDs
    real_fe_mask = _fe_mask & df["movie_file_id"].notna()
    franchise_file_ids.update(df.loc[real_fe_mask, "movie_file_id"].dropna())

    # Collections already covered by a real franchise entry
    real_fe_collections: set = set()
    if "collection_name" in df.columns:
        real_fe_collections = set(
            df.loc[real_fe_mask, "collection_name"].dropna().unique()
        )

    # 2. De-facto franchise for collections without a resolved franchise entry
    if "is_watched" not in df.columns or "collection_name" not in df.columns:
        return frozenset(franchise_file_ids)

    watched_mask = (
        df["is_watched"].infer_objects(copy=False).fillna(False).astype(bool)
    )
    candidate_mask = watched_mask & ~_fe_mask
    if not candidate_mask.any():
        return frozenset(franchise_file_ids)

    candidates = df[candidate_mask]
    for coll_name in candidates["collection_name"].dropna().unique():
        if coll_name in real_fe_collections:
            continue
        coll_mask = candidates["collection_name"] == coll_name
        coll_rows = candidates[coll_mask]
        if coll_rows.empty:
            continue
        _years = pd.to_numeric(coll_rows["year"], errors="coerce").fillna(9999)
        earliest_idx = _years.idxmin()
        fid = coll_rows.at[earliest_idx, "movie_file_id"] if earliest_idx in coll_rows.index else None
        if fid is not None and pd.notna(fid):
            franchise_file_ids.add(fid)

    return frozenset(franchise_file_ids)
