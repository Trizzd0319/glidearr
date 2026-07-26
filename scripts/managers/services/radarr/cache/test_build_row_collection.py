"""_build_row collection extraction — Radarr v4/v5 send ``collection`` as
``{title, tmdbId}`` (NO 'name' key); v3 sent ``{name}``. The row builder must read
BOTH keys, else ``collection_name`` is all-NULL on modern instances — the bug that
silently starved the C1/C2 collection scoring signals, the active-collection
downgrade guard, the saga-credit collection features and the de-facto franchise
anchors until the 2026-07 fix (title-or-name, matching the tagless resolver's
``_movie_collection_label``)."""
from __future__ import annotations

from scripts.managers.services.radarr.cache.movie_files import RadarrCacheMovieFilesManager


def _row(movie):
    m = object.__new__(RadarrCacheMovieFilesManager)   # skip __init__/registry/base
    return m._build_row(
        movie=movie,
        movie_file={"id": 7, "size": 1000},
        watch_data={},
        tag_label_map={},
        quality_profile_map={},
        is_franchise_entry=False,
        keep_policy=None,
    )


def test_v4_v5_collection_title_key_populates_collection_name():
    row = _row({"id": 1, "tmdbId": 603,
                "collection": {"title": "The Matrix Collection", "tmdbId": 2344}})
    assert row["collection_name"] == "The Matrix Collection"
    assert row["collection_tmdb_id"] == 2344


def test_v3_collection_name_key_still_works():
    row = _row({"id": 2, "tmdbId": 564,
                "collection": {"name": "The Mummy Collection", "tmdbId": 1733}})
    assert row["collection_name"] == "The Mummy Collection"
    assert row["collection_tmdb_id"] == 1733


def test_both_keys_prefer_title():
    row = _row({"id": 3, "tmdbId": 605,
                "collection": {"title": "New Title", "name": "Old Name", "tmdbId": 9}})
    assert row["collection_name"] == "New Title"


def test_missing_or_empty_collection_stays_none():
    assert _row({"id": 4, "tmdbId": 606})["collection_name"] is None
    row = _row({"id": 5, "tmdbId": 607, "collection": {}})
    assert row["collection_name"] is None and row["collection_tmdb_id"] is None
