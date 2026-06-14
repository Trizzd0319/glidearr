"""Tests for features.movie_features — the movie row -> MovieFeatureRow -> score adapter.

The B1 batch-scoring speedup feeds ``build_movie_feature_row`` a plain dict row (from one
``df.to_dict("records")`` pass) instead of a fresh ``df.loc[idx]`` Series per row. This locks
that the two row sources are BYTE-IDENTICAL: the adapter reads every field through
``row.get(col)`` + ``pd.notna()`` coercion, so a Series row and a records-dict row yield the
exact same ``MovieFeatureRow`` — and therefore the exact same score. A mixed-dtype frame is
used deliberately so ``df.loc[idx]`` object-upcasts the row (the case most likely to differ).
"""
from __future__ import annotations

import json

import pandas as pd

from scripts.managers.machine_learning.features.movie_features import (
    build_movie_feature_row,
    score_movie_features,
)


def _frame():
    # Mixed dtypes + NaN across every column the adapter reads, so df.loc[idx] upcasts.
    return pd.DataFrame([
        dict(  # fully-populated, watched, acclaimed, franchise
            tmdb_id=101, percent_complete=95.0, watch_count=3,
            genres=json.dumps(["Action", "Sci-Fi"]),
            collection_tmdb_id=900, collection_name="Saga",
            imdb_rating=8.6, tmdb_rating=8.1, trakt_rating=82.0,
            rotten_tomatoes_score=91.0, metacritic_score=78.0, popularity=120.0,
            certification="PG-13", original_language="en",
            in_cinemas_date="2024-01-01", physical_release_date="2024-03-01",
            digital_release_date="2024-02-15", keep_policy="keep_movie",
            is_franchise_entry=True, universe_name="MCU", is_available=True,
        ),
        dict(  # sparse / never-watched / NaN-heavy, foreign language, unavailable
            tmdb_id=102, percent_complete=0.0, watch_count=0,
            genres=None, collection_tmdb_id=float("nan"), collection_name=None,
            imdb_rating=float("nan"), tmdb_rating=float("nan"), trakt_rating=float("nan"),
            rotten_tomatoes_score=float("nan"), metacritic_score=float("nan"),
            popularity=float("nan"), certification=None, original_language="fr",
            in_cinemas_date=None, physical_release_date=None, digital_release_date=None,
            keep_policy=None, is_franchise_entry=False, universe_name=None, is_available=False,
        ),
        dict(  # partial — some ratings, abandoned watch
            tmdb_id=103, percent_complete=12.0, watch_count=1,
            genres=json.dumps(["Horror"]),
            collection_tmdb_id=float("nan"), collection_name=None,
            imdb_rating=3.2, tmdb_rating=float("nan"), trakt_rating=float("nan"),
            rotten_tomatoes_score=float("nan"), metacritic_score=float("nan"),
            popularity=15.0, certification="R", original_language="en",
            in_cinemas_date="2010-05-05", physical_release_date=None,
            digital_release_date=None, keep_policy=None,
            is_franchise_entry=False, universe_name=None, is_available=True,
        ),
    ])


def test_series_row_and_dict_row_build_identical_feature_rows():
    df = _frame()
    records = df.to_dict("records")
    for i, idx in enumerate(df.index):
        fr_series = build_movie_feature_row(df.loc[idx], credits={}, related_tmdb_ids=None)
        fr_dict = build_movie_feature_row(records[i], credits={}, related_tmdb_ids=None)
        # frozen dataclass -> field-wise equality
        assert fr_series == fr_dict, f"row {idx}: {fr_series} != {fr_dict}"


def test_series_row_and_dict_row_score_identically():
    df = _frame()
    records = df.to_dict("records")
    ctx = dict(genre_affinity={}, watched_tmdb_ids=set(), collection_members={})
    for i, idx in enumerate(df.index):
        s_series = score_movie_features(
            build_movie_feature_row(df.loc[idx], credits={}), **ctx)
        s_dict = score_movie_features(
            build_movie_feature_row(records[i], credits={}), **ctx)
        assert s_series == s_dict


def test_feature_row_coercion_values():
    # spot-check the coercion itself: fraction conversion, NaN -> None/defaults, JSON genres.
    df = _frame()
    fr = build_movie_feature_row(df.loc[0], credits={})
    assert fr.tmdb_id == 101 and fr.percent_complete == 0.95 and fr.watch_count == 3
    assert fr.genres == ("Action", "Sci-Fi") and fr.is_franchise_entry is True
    fr2 = build_movie_feature_row(df.loc[1], credits={})
    assert fr2.imdb_rating is None and fr2.genres == () and fr2.collection_name is None
    assert fr2.percent_complete == 0.0 and fr2.is_available is False
