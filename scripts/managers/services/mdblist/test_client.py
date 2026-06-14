"""Tests for mdblist.client.validate_key — auth + tier parsing, alias tolerance, never-raises.
The HTTP is stubbed via the isolated _http_get seam, so these run with no network."""
from __future__ import annotations

from scripts.managers.services.mdblist import client


def _stub(status, body, headers=None):
    return lambda url, params, timeout: (status, headers or {}, body)


def test_no_apikey_is_skip():
    r = client.validate_key("")
    assert r["ok"] is False and r["error"] == "no apikey"


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
