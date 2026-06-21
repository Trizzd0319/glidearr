"""
features/movie_features.py — the movie row -> MovieFeatureRow -> score adapter.
================================================================================
The single boundary between the Radarr movie_files cache and the pure movie
scorer (ML Step 3c). ``build_movie_feature_row`` marshals a Parquet row's COLUMN
NAMES into the typed ``MovieFeatureRow`` (this is the ONE place the cache schema is
known); ``score_movie_features`` reconstructs the exact ``score_movie`` call from a
feature row + the shared library context. PURE — no HTTP, no global_cache, no I/O:
the service does the cache reads (credits, related set, affinity maps) and passes
them in.

Public API:
  * build_movie_feature_row(row, *, credits=None, related_tmdb_ids=None) -> MovieFeatureRow
  * score_movie_features(fr, *, <library context>, return_breakdown=False) -> int | (int, dict)
"""
from __future__ import annotations

import json

import pandas as pd

from scripts.managers.machine_learning.contracts.feature_rows import MovieFeatureRow
from scripts.managers.machine_learning.scoring._shared import preferred_language_available
from scripts.managers.machine_learning.scoring.movie_scorer import score_movie


def build_movie_feature_row(
    row,
    *,
    credits: dict | None = None,
    related_tmdb_ids=None,
) -> MovieFeatureRow:
    """Marshal a movie_files Parquet row into a MovieFeatureRow. ``credits`` (Trakt
    people) and ``related_tmdb_ids`` (daemon-cached C3 neighbours) are fetched by the
    service (I/O) and passed in. ``percent_complete`` is stored as a 0-1 fraction.
    Mirrors the column reads previously inlined in space_pressure._score_row."""
    def _f(col):
        v = row.get(col)
        return float(v) if pd.notna(v) and v is not None else None

    def _s(col):
        v = row.get(col)
        return str(v) if pd.notna(v) and v else None

    def _b(col):
        v = row.get(col)
        return bool(v) if pd.notna(v) and v is not None else False

    tmdb_raw = row.get("tmdb_id")
    pct_raw = row.get("percent_complete")
    wc_raw = row.get("watch_count")
    genres_raw = row.get("genres")
    try:
        genres = json.loads(genres_raw) if genres_raw and pd.notna(genres_raw) else []
    except Exception:
        genres = []
    coll_tmdb_raw = row.get("collection_tmdb_id")
    coll_name = row.get("collection_name")

    return MovieFeatureRow(
        tmdb_id=int(tmdb_raw) if pd.notna(tmdb_raw) else None,
        genres=tuple(genres),
        percent_complete=float(pct_raw) / 100.0 if pd.notna(pct_raw) else 0.0,
        watch_count=int(wc_raw) if wc_raw and pd.notna(wc_raw) else 0,
        credits=credits or {},
        imdb_rating=_f("imdb_rating"),
        tmdb_rating=_f("tmdb_rating"),
        trakt_rating=_f("trakt_rating"),
        rotten_tomatoes_score=_f("rotten_tomatoes_score"),
        metacritic_score=_f("metacritic_score"),
        popularity=_f("popularity"),
        certification=_s("certification"),
        original_language=_s("original_language"),
        in_cinemas_date=_s("in_cinemas_date"),
        physical_release_date=_s("physical_release_date"),
        digital_release_date=_s("digital_release_date"),
        keep_policy=_s("keep_policy"),
        is_franchise_entry=_b("is_franchise_entry"),
        universe_name=_s("universe_name"),
        is_available=_b("is_available"),
        # single-file movie: consumable fraction is 1.0 (has en dub/sub) or 0.0; None
        # when no track data so G1 falls back to the legacy penalty (byte-identical).
        language_consumable_fraction=(
            (1.0 if preferred_language_available(_s("audio_languages"), _s("subtitles"), ["en"]) else 0.0)
            if (_s("audio_languages") or _s("subtitles")) else None
        ),
        collection_tmdb_id=int(coll_tmdb_raw) if pd.notna(coll_tmdb_raw) else None,
        collection_name=coll_name if pd.notna(coll_name) and coll_name else None,
        related_tmdb_ids=tuple(related_tmdb_ids) if related_tmdb_ids is not None else None,
    )


def score_movie_features(
    fr: MovieFeatureRow,
    *,
    genre_affinity: dict,
    watched_tmdb_ids,
    collection_members: dict,
    platform_usage: dict | None = None,
    transcode_stats: dict | None = None,
    per_user_affinity: dict | None = None,
    kids_users: list | None = None,
    adult_users: list | None = None,
    completion_threshold: float = 0.9,
    affinity_boost: float = 1.0,
    related_graph_cap: float = 4.0,
    person_weights: dict | None = None,
    person_affinity_cap: float = 0.0,
    language_consumability: bool = False,
    return_breakdown: bool = False,
):
    """Reconstruct the exact ``score_movie`` call from a MovieFeatureRow + the shared
    library context. Byte-identical to the marshalling previously inline in
    space_pressure._score_row. ``person_weights``/``person_affinity_cap`` feed Group-C4
    (cast/crew taste overlap); cap DEFAULT 0.0 → C4 contributes 0.0 → byte-identical until
    a caller opts in (space_pressure gates it on config + a built people-matrix)."""
    movie = {
        "tmdbId": fr.tmdb_id,
        "genres": list(fr.genres),
        "collection": (
            {"tmdbId": fr.collection_tmdb_id, "name": fr.collection_name}
            if fr.collection_name is not None else {}
        ),
    }
    related = set(fr.related_tmdb_ids) if fr.related_tmdb_ids is not None else None
    return score_movie(
        movie=movie,
        completion_pct=fr.percent_complete,
        completion_threshold=completion_threshold,
        collection_members=collection_members,
        watched_tmdb_ids=watched_tmdb_ids,
        genre_affinity=genre_affinity,
        credits=fr.credits,
        watch_count=fr.watch_count,
        platform_usage=platform_usage,
        transcode_stats=transcode_stats,
        per_user_affinity=per_user_affinity,
        kids_users=kids_users,
        adult_users=adult_users,
        imdb_rating=fr.imdb_rating,
        tmdb_rating=fr.tmdb_rating,
        trakt_rating=fr.trakt_rating,
        metacritic_score=fr.metacritic_score,
        rotten_tomatoes_score=fr.rotten_tomatoes_score,
        popularity=fr.popularity,
        certification=fr.certification,
        in_cinemas_date=fr.in_cinemas_date,
        physical_release_date=fr.physical_release_date,
        digital_release_date=fr.digital_release_date,
        original_language=fr.original_language,
        # File-aware G1 is OPT-IN (oracle-mover): pass the consumable fraction only when
        # enabled, else None → legacy household-language penalty → byte-identical.
        language_consumable_fraction=(fr.language_consumable_fraction if language_consumability else None),
        is_franchise_entry=fr.is_franchise_entry,
        universe_name=fr.universe_name,
        keep_policy=fr.keep_policy,
        is_available=fr.is_available,
        affinity_boost=affinity_boost,
        related_tmdb_ids=related,
        related_graph_cap=related_graph_cap,
        person_weights=person_weights,
        person_affinity_cap=person_affinity_cap,
        return_breakdown=return_breakdown,
    )
