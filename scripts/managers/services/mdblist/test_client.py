"""Tests for mdblist.client.validate_key — auth + tier parsing, alias tolerance, never-raises.
The HTTP is stubbed via the isolated _http_get seam, so these run with no network."""
from __future__ import annotations

from scripts.managers.services.mdblist import client


def _stub(status, body, headers=None):
    return lambda url, params, timeout: (status, headers or {}, body)


def test_no_apikey_is_skip():
    r = client.validate_key("")
    assert r["ok"] is False and r["error"] == "no apikey"


# ── list_items ────────────────────────────────────────────────────────────────
def test_list_items_no_apikey_or_ref():
    assert client.list_items("", {"imdb": "ls1"})["ok"] is False
    assert client.list_items("k", {})["ok"] is False


def test_list_items_parses_split_movies_shows_in_order(monkeypatch):
    monkeypatch.setattr(client, "_http_get", _stub(200, {
        "movies": [{"tmdb_id": 1726, "mediatype": "movie"},      # Iron Man
                   {"tmdb_id": 1724}],                            # no mediatype → inferred movie
        "shows": [{"tvdb_id": 280619, "mediatype": "show"}],     # Agent Carter
    }))
    r = client.list_items("k", {"imdb": "ls539646485"})
    assert r["ok"] is True
    assert r["items"] == [{"tmdb": 1726, "tvdb": None, "media": "movie"},
                          {"tmdb": 1724, "tvdb": None, "media": "movie"},
                          {"tmdb": None, "tvdb": 280619, "media": "show"}]


def test_list_items_bare_list_and_alias_tolerant(monkeypatch):
    monkeypatch.setattr(client, "_http_get", _stub(200, [
        {"tmdb": 603, "type": "movie"}, {"tvdbid": 78901, "media_type": "tv"}, {"junk": 1}]))
    r = client.list_items("k", {"mdblist": "k0meta/external/15110"})
    assert r["items"] == [{"tmdb": 603, "tvdb": None, "media": "movie"},
                          {"tmdb": None, "tvdb": 78901, "media": "show"}]   # junk row dropped


def test_list_items_http_error_degrades(monkeypatch):
    monkeypatch.setattr(client, "_http_get", _stub(404, None))
    r = client.list_items("k", {"id": 15110})
    assert r["ok"] is False and r["items"] == []


def test_list_items_malformed_body_soft_degrades_not_raises(monkeypatch):
    # REGRESSION (review HIGH): a shape-drifted body where movies/shows is a non-iterable scalar
    # must NOT raise (caller relies on never-raises to keep its last-good cache).
    for bad in ({"movies": 5}, {"shows": 3.5}, {"movies": True}, 42, "nope"):
        monkeypatch.setattr(client, "_http_get", _stub(200, bad))
        r = client.list_items("k", {"imdb": "ls1"})
        assert r["ok"] is True and r["items"] == []        # degrades to empty, no exception


def test_list_items_non_dict_ref_does_not_raise():
    # REGRESSION (review MEDIUM): an operator config typo ({"mcu": "ls539646485"}) makes ref a
    # bare string → must return ok=False, not AttributeError.
    assert client.list_items("k", "ls539646485")["ok"] is False
    assert client.list_items("k", ["x"])["ok"] is False


def test_list_items_movie_row_with_only_tvdb_becomes_show(monkeypatch):
    monkeypatch.setattr(client, "_http_get", _stub(200, [{"tvdb_id": 95057, "mediatype": "movie"}]))
    r = client.list_items("k", {"imdb": "ls1"})
    assert r["items"] == [{"tmdb": None, "tvdb": 95057, "media": "show"}]   # not dropped


def test_list_items_reads_tmdb_from_id_and_ids(monkeypatch):
    # REGRESSION: mdblist rows put TMDB in `id` / nested `ids`, NOT `tmdb_id` — a movie row must
    # classify as a MOVIE (a prior bug read only tmdb_id → tmdb None → every movie misfiled a show).
    rows = {"movies": [{"id": 603, "mediatype": "movie", "tvdb_id": 70, "ids": {"tmdb": 603, "tvdb": 70}}],
            "shows":  [{"id": 1396, "mediatype": "show", "tvdb_id": 81189, "ids": {"tmdb": 1396, "tvdb": 81189}}]}
    monkeypatch.setattr(client, "_http_get", _stub(200, rows))
    r = client.list_items("k", {"id": 1})
    assert r["items"] == [{"tmdb": 603, "tvdb": 70, "media": "movie"},
                          {"tmdb": 1396, "tvdb": 81189, "media": "show"}]


def test_list_items_captures_titles_keyed_media_id(monkeypatch):
    # mdblist rows carry a display title; list_items surfaces them keyed "<media>:<id>" so an UNOWNED
    # universe movie still resolves to a real name downstream (the saga preview), no extra API call.
    monkeypatch.setattr(client, "_http_get", _stub(200, {
        "movies": [{"id": 1726, "mediatype": "movie", "title": "Iron Man"},
                   {"id": 24428, "mediatype": "movie", "title": "The Avengers"}],
        "shows": [{"tvdb_id": 280619, "mediatype": "show", "title": "Agent Carter"}],
    }))
    r = client.list_items("k", {"id": 117444})
    assert r["items"][0] == {"tmdb": 1726, "tvdb": None, "media": "movie"}   # item shape unchanged (ids only)
    assert r["titles"] == {"movie:1726": "Iron Man", "movie:24428": "The Avengers",
                           "show:280619": "Agent Carter"}


def test_list_items_titles_empty_when_rows_have_none(monkeypatch):
    monkeypatch.setattr(client, "_http_get", _stub(200, [{"tmdb_id": 1726, "mediatype": "movie"}]))
    assert client.list_items("k", {"id": 1})["titles"] == {}            # no title field → no entry, no crash


def test_supporter_tier_with_budget(monkeypatch):
    monkeypatch.setattr(client, "_http_get", _stub(200, {
        "username": "trizzd", "patron_status": "active_patron",
        "api_requests": 1000, "api_requests_count": 12,
    }))
    r = client.validate_key("k")
    assert r["ok"] and r["username"] == "trizzd" and r["tier"] == "supporter"
    assert r["limit"] == 1000 and r["used"] == 12


def test_free_tier(monkeypatch):
    monkeypatch.setattr(client, "_http_get", _stub(200, {
        "username": "joe", "patron_status": "inactive", "api_requests": 100,
    }))
    r = client.validate_key("k")
    assert r["tier"] == "free" and r["limit"] == 100 and r["used"] is None


def test_unknown_tier_when_no_patron_field(monkeypatch):
    monkeypatch.setattr(client, "_http_get", _stub(200, {"username": "x"}))
    r = client.validate_key("k")
    assert r["ok"] and r["tier"] == "unknown"


def test_field_aliases(monkeypatch):
    # alternate field names must still resolve (tolerant parsing)
    monkeypatch.setattr(client, "_http_get", _stub(200, {
        "user": "a", "is_supporter": True, "daily_limit": 500, "used": 3,
    }))
    r = client.validate_key("k")
    assert r["username"] == "a" and r["tier"] == "supporter" and r["limit"] == 500 and r["used"] == 3


def test_header_budget_fallback(monkeypatch):
    monkeypatch.setattr(client, "_http_get", _stub(
        200, {"username": "h"}, {"X-RateLimit-Limit": "1000", "X-RateLimit-Remaining": "900"}))
    r = client.validate_key("k")
    assert r["limit"] == 1000 and r["used"] == 100   # used = limit - remaining


def test_invalid_key_401(monkeypatch):
    monkeypatch.setattr(client, "_http_get", _stub(401, None))
    r = client.validate_key("k")
    assert r["ok"] is False and "invalid key" in r["error"]


def test_other_http_status(monkeypatch):
    monkeypatch.setattr(client, "_http_get", _stub(503, None))
    assert client.validate_key("k")["ok"] is False


def test_non_json_body(monkeypatch):
    monkeypatch.setattr(client, "_http_get", _stub(200, None))
    assert client.validate_key("k")["ok"] is False


def test_never_raises_on_network_error(monkeypatch):
    def _boom(url, params, timeout):
        raise RuntimeError("net down")
    monkeypatch.setattr(client, "_http_get", _boom)
    r = client.validate_key("k")
    assert r["ok"] is False and "net down" in r["error"]


# ── movie / show ratings (Common Sense age) ──────────────────────────────────────

def test_movie_ratings_hits_movie_path_and_parses_age(monkeypatch):
    seen = {}

    def _capture(url, params, timeout):
        seen["url"] = url
        return 200, {}, {"age_rating": 8, "commonsense": True, "certification": "PG"}
    monkeypatch.setattr(client, "_http_get", _capture)
    r = client.movie_ratings("k", 603)
    assert seen["url"].endswith("/tmdb/movie/603")           # MOVIE endpoint
    assert r["ok"] and r["age_rating"] == 8 and r["certification"] == "PG"


def test_show_ratings_hits_show_path(monkeypatch):
    seen = {}

    def _capture(url, params, timeout):
        seen["url"] = url
        return 200, {}, {"age_rating": 5, "commonsense": True, "certification": None}
    monkeypatch.setattr(client, "_http_get", _capture)
    r = client.show_ratings("k", 82728)
    assert seen["url"].endswith("/tmdb/show/82728")          # SHOW endpoint (not /movie)
    assert r["ok"] and r["age_rating"] == 5


def test_ratings_missing_inputs_and_http_error(monkeypatch):
    assert client.show_ratings("", 1)["ok"] is False         # no apikey
    assert client.movie_ratings("k", None)["ok"] is False    # no id
    monkeypatch.setattr(client, "_http_get", _stub(500, None))
    assert client.show_ratings("k", 1)["ok"] is False and client.show_ratings("k", 1)["age_rating"] is None
