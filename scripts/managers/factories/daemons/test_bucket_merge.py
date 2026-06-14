"""Hand-verified unit tests for bucket_merge (daemon bucket → parquet columns)."""
import json

from scripts.managers.factories.daemons import bucket_merge as bm


PEOPLE = {
    "cast": [
        {"name": "Bryan Cranston", "order": 0},
        {"name": "Aaron Paul", "order": 1},
        {"name": "", "order": 2},            # blank dropped
    ],
    "crew": [
        {"name": "Vince Gilligan", "job": "Director", "department": "Directing"},
        {"name": "Vince Gilligan", "job": "Writer", "department": "Writing"},
        {"name": "Dave Porter", "job": "Original Music Composer", "department": "Sound"},
        {"name": "Mark Johnson", "job": "Executive Producer", "department": "Production"},
    ],
}


def test_flatten_people():
    c = bm.flatten_trakt_people(PEOPLE)
    assert c["cast_names"] == "Bryan Cranston|Aaron Paul"     # ordered, blank dropped
    assert c["director_names"] == "Vince Gilligan"
    assert c["writer_names"] == "Vince Gilligan"
    assert c["producer_names"] == "Mark Johnson"
    assert c["composer_names"] == "Dave Porter"


def test_flatten_people_cast_limit_and_dedupe():
    p = {"cast": [{"name": f"A{i}", "order": i} for i in range(15)], "crew": []}
    assert len(bm.flatten_trakt_people(p)["cast_names"].split("|")) == 10   # top-10
    dup = {"cast": [{"name": "X", "order": 0}, {"name": "X", "order": 1}], "crew": []}
    assert bm.flatten_trakt_people(dup)["cast_names"] == "X"                # deduped


def test_flatten_empty():
    c = bm.flatten_trakt_people({})
    assert all(v is None for v in c.values())
    assert bm.flatten_trakt_people(None)["cast_names"] is None


def test_genres_priority():
    # Sonarr list wins over daemon summary
    assert json.loads(bm.genres_json(["Drama", "Crime"], {"genres": ["Thriller"]})) == ["Drama", "Crime"]
    # falls back to daemon summary dict
    assert json.loads(bm.genres_json(None, {"genres": ["Thriller"]})) == ["Thriller"]
    assert bm.genres_json(None, {}) is None
    assert bm.genres_json([], {"genres": []}) is None


def test_trakt_rating_cols():
    assert bm.trakt_rating_cols({"rating": 8.7, "votes": 1234}) == {"trakt_rating": 8.7, "trakt_vote_count": 1234}
    assert bm.trakt_rating_cols({}) == {"trakt_rating": None, "trakt_vote_count": None}


def test_show_enrichment_columns_combines_all():
    cols = bm.show_enrichment_columns(
        people=PEOPLE, ratings={"rating": 9.0, "votes": 10},
        summary={"genres": ["Sci-Fi"]}, sonarr_genres=["Drama", "Crime"],
    )
    assert cols["cast_names"] == "Bryan Cranston|Aaron Paul"
    assert json.loads(cols["genres"]) == ["Drama", "Crime"]      # Sonarr wins
    assert cols["trakt_rating"] == 9.0
    # parquet-safe primitives only
    assert all(v is None or isinstance(v, (str, int, float)) for v in cols.values())
