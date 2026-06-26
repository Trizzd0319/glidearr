"""Tests for PlexAPI hardening: URL scrubbing, base-url build, header contract,
and the 429/Retry-After + transient-retry discipline (mirrors Trakt/Tautulli)."""
from __future__ import annotations

import requests

from scripts.managers.services.plex.instances import api as api_mod
from scripts.managers.services.plex.instances.api import PlexAPI, build_base_url, scrub_url


class _Logger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass


class _Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.content = b"x" if body is not None else b""
        self.url = "https://plex.tv/api/v2/user?X-Plex-Token=SECRET"
    def json(self): return self._body
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")


class _Session:
    """Replays a scripted list of responses (or exceptions) per call."""
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.headers = {}
    def request(self, **kwargs):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    def close(self): pass


def _api(script):
    a = PlexAPI(logger=_Logger(), instance_config={"plex_token": "T", "url": "h", "port": 32400},
                client_identifier="cid-1")
    a._session = _Session(script)
    return a


# ── pure helpers ─────────────────────────────────────────────────────────────
def test_scrub_url_drops_query():
    assert scrub_url("https://plex.tv/api/v2/user?X-Plex-Token=SECRET&a=1") == "https://plex.tv/api/v2/user"


def test_build_base_url():
    assert build_base_url("1.2.3.4", 32400) == "http://1.2.3.4:32400"
    assert build_base_url("https://plex.lan", 1) == "https://plex.lan"


def test_headers_contract():
    a = _api([_Resp(200, {"ok": 1})])
    h = a._headers("OVERRIDE")
    assert h["X-Plex-Token"] == "OVERRIDE"
    assert h["X-Plex-Client-Identifier"] == "cid-1"
    assert h["Accept"] == "application/json"


# ── 429 / retry discipline ────────────────────────────────────────────────────
def test_429_then_success(monkeypatch):
    monkeypatch.setattr(api_mod.time, "sleep", lambda *_: None)
    a = _api([_Resp(429, headers={"Retry-After": "1"}), _Resp(200, {"ok": 1})])
    out = a.get_account()
    assert out == {"ok": 1} and a._session.calls == 2


def test_429_over_cap_serves_fallback(monkeypatch):
    monkeypatch.setattr(api_mod.time, "sleep", lambda *_: None)
    a = _api([_Resp(429, headers={"Retry-After": "999"})])
    assert a.get_account(fallback="FB") == "FB"
    assert a.rate_limited is True


def test_401_returns_fallback_no_retry():
    a = _api([_Resp(401, {"err": 1})])
    assert a.get_account(fallback=None) is None
    assert a._session.calls == 1                      # never falls through to broader scope


# ── playlist WRITE verbs (PR-2: verbs only — unreferenced until the writeback pass) ──
class _CapSession:
    """Like _Session but captures each request()'s kwargs so we can assert verb/url/params."""
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.headers = {}
    def request(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    def close(self): pass


def _cap_api(script):
    a = PlexAPI(logger=_Logger(), instance_config={"plex_token": "T", "url": "h", "port": 32400},
                client_identifier="cid-1")
    a._session = _CapSession(script)
    return a


def test_get_machine_id_parses_and_caches():
    a = _cap_api([_Resp(200, {"MediaContainer": {"machineIdentifier": "MID-123", "version": "1.0"}})])
    assert a.get_machine_id() == "MID-123"
    assert a.get_machine_id() == "MID-123"             # cached — no second identity fetch
    assert len(a._session.calls) == 1


def test_create_playlist_puts_items_in_uri_query_not_body():
    a = _cap_api([_Resp(200, {})])
    a._machine_id = "MID"                               # prime so no identity round-trip
    a.create_playlist("Aiden Up Next", ["10", "20", "30"], token="USERTOK")
    call = a._session.calls[0]
    assert call["method"] == "POST" and call["url"].endswith("/playlists")
    p = call["params"]
    assert p["type"] == "video" and p["title"] == "Aiden Up Next" and p["smart"] == 0
    assert p["uri"] == "server://MID/com.plexapp.plugins.library/library/metadata/10,20,30"
    assert "json" not in call and "data" not in call   # items ride the QUERY param (red-team CRITICAL)
    assert call["headers"]["X-Plex-Token"] == "USERTOK"   # per-user scoping → member's account


def test_add_playlist_items_uri():
    a = _cap_api([_Resp(200, {})])
    a._machine_id = "MID"
    a.add_playlist_items("777", ["5", "6"], token="UT")
    call = a._session.calls[0]
    assert call["method"] == "PUT" and call["url"].endswith("/playlists/777/items")
    assert call["params"]["uri"] == "server://MID/com.plexapp.plugins.library/library/metadata/5,6"
    assert "json" not in call and "data" not in call


def test_remove_playlist_item_uses_playlist_item_id():
    a = _cap_api([_Resp(200, {})])
    a.remove_playlist_item("777", "9001", token="UT")
    call = a._session.calls[0]
    assert call["method"] == "DELETE" and call["url"].endswith("/playlists/777/items/9001")


def test_move_playlist_item_after_and_to_front():
    a = _cap_api([_Resp(200, {}), _Resp(200, {})])
    a.move_playlist_item("777", "9001", after_id="9000", token="UT")
    a.move_playlist_item("777", "9001", token="UT")    # no after → move to front
    c0, c1 = a._session.calls
    assert c0["method"] == "PUT" and c0["url"].endswith("/playlists/777/items/9001/move")
    assert c0["params"] == {"after": "9000"}
    assert c1["params"] is None


def test_delete_playlist():
    a = _cap_api([_Resp(200, {})])
    a.delete_playlist("777", token="UT")
    call = a._session.calls[0]
    assert call["method"] == "DELETE" and call["url"].endswith("/playlists/777")


def test_upload_playlist_poster_posts_bytes_to_library_metadata():
    a = _cap_api([_Resp(200, {})])
    ok = a.upload_playlist_poster("777", b"\x89PNG-data", token="UT")
    assert ok is True                                      # 2xx → verified success
    call = a._session.calls[0]
    # The playlist ratingKey resolves under /library/metadata (the /playlists/{rk}/posters path 404s).
    assert call["method"] == "POST" and call["url"].endswith("/library/metadata/777/posters")
    assert call["data"] == b"\x89PNG-data"                # bytes ride the BODY, not a uri param
    assert "params" not in call or call["params"] is None  # no query items on a poster upload
    assert call["headers"]["Content-Type"] == "image/png"
    assert call["headers"]["X-Plex-Token"] == "UT"        # per-user scope → member's own list


def test_upload_playlist_poster_false_on_404():
    # The 404 the OLD endpoint returned must surface as failure, not a silent success.
    a = _cap_api([_Resp(404, {})])
    assert a.upload_playlist_poster("777", b"\x89PNG-data", token="UT") is False


def test_upload_playlist_poster_skips_empty_bytes():
    a = _cap_api([])                                       # no scripted response → must not call out
    assert a.upload_playlist_poster("777", b"", token="UT") is False
    assert a._session.calls == []


def test_edit_playlist_sets_title_and_locked_titlesort():
    a = _cap_api([_Resp(200, {})])
    ok = a.edit_playlist("777", title="Up Next", title_sort="!Up Next", token="UT")
    assert ok is True
    call = a._session.calls[0]
    assert call["method"] == "PUT" and call["url"].endswith("/playlists/777")
    # The plain titleSort param is ignored by PMS — the LOCKED field form is required.
    assert call["params"] == {"title": "Up Next", "titleSort.value": "!Up Next", "titleSort.locked": 1}
    assert call["headers"]["X-Plex-Token"] == "UT"        # per-user scope → member's own list


def test_edit_playlist_false_on_404():
    a = _cap_api([_Resp(404, {})])
    assert a.edit_playlist("777", title="Up Next", title_sort="!Up Next", token="UT") is False


def test_edit_playlist_noop_when_nothing_to_set():
    a = _cap_api([])
    assert a.edit_playlist("777", token="UT") is False
    assert a._session.calls == []


# ── library COLLECTIONS write + Home promotion (managed-hub API) ──────────────
def test_create_collection_section_scoped_items_in_uri_query():
    a = _cap_api([_Resp(200, {})])
    a._machine_id = "MID"
    a.create_collection("2", "Up Next - Household", ["10", "20", "30"])
    call = a._session.calls[0]
    assert call["method"] == "POST" and call["url"].endswith("/library/collections")
    p = call["params"]
    assert p["type"] == 1 and p["smart"] == 0 and p["sectionId"] == "2"
    assert p["title"] == "Up Next - Household"
    assert p["uri"] == "server://MID/com.plexapp.plugins.library/library/metadata/10,20,30"
    assert "json" not in call and "data" not in call             # items ride the QUERY param


def test_add_collection_items_uri():
    a = _cap_api([_Resp(200, {})])
    a._machine_id = "MID"
    a.add_collection_items("555", ["7", "8"])
    call = a._session.calls[0]
    assert call["method"] == "PUT" and call["url"].endswith("/library/collections/555/items")
    assert call["params"]["uri"] == "server://MID/com.plexapp.plugins.library/library/metadata/7,8"


def test_remove_collection_item_by_rating_key():
    a = _cap_api([_Resp(200, {})])
    a.remove_collection_item("555", "8")
    call = a._session.calls[0]
    assert call["method"] == "DELETE" and call["url"].endswith("/library/collections/555/items/8")


def test_promote_collection_home_keys_by_metadata_item_id():
    a = _cap_api([_Resp(200, {})])
    a.promote_collection_home("2", "555", home=True, shared=False)
    call = a._session.calls[0]
    assert call["method"] == "POST" and call["url"].endswith("/hubs/sections/2/manage")
    p = call["params"]
    assert p["metadataItemId"] == "555"                          # not-yet-managed hub keyed by rk
    assert p["promotedToOwnHome"] == 1 and p["promotedToSharedHome"] == 0
    assert p["promotedToRecommended"] == 0


def test_get_managed_hubs_section_scoped():
    a = _cap_api([_Resp(200, {"MediaContainer": {}})])
    a.get_managed_hubs("2")
    call = a._session.calls[0]
    assert call["method"] == "GET" and call["url"].endswith("/hubs/sections/2/manage")


# ── per-server access token (the managed-user write enabler) ──────────────────
def test_get_resources_is_external_and_token_scoped():
    a = _cap_api([_Resp(200, [{"clientIdentifier": "MID", "accessToken": "ACC"}])])
    out = a.get_resources("USERAUTH", fallback=[])
    call = a._session.calls[0]
    assert call["method"] == "GET" and call["url"].endswith("/api/v2/resources")
    assert call["headers"]["X-Plex-Token"] == "USERAUTH"          # scoped to the user's account
    assert out == [{"clientIdentifier": "MID", "accessToken": "ACC"}]


def test_server_access_token_picks_matching_machine_id():
    # resources lists several servers/players; pick the one == our PMS machineIdentifier.
    a = _cap_api([_Resp(200, [
        {"clientIdentifier": "OTHER", "accessToken": "wrong"},
        {"clientIdentifier": "MID", "accessToken": "ACC"},
    ])])
    a._machine_id = "MID"                                          # prime so no /identity round-trip
    assert a.server_access_token("USERAUTH") == "ACC"


def test_server_access_token_absent_server_returns_fallback():
    a = _cap_api([_Resp(200, [{"clientIdentifier": "OTHER", "accessToken": "wrong"}])])
    a._machine_id = "MID"
    assert a.server_access_token("USERAUTH", fallback=None) is None


def test_404_returns_fallback():
    a = _api([_Resp(404)])
    assert a.get_sections(fallback=[]) == []


def test_transient_error_retries_with_fresh_session(monkeypatch):
    monkeypatch.setattr(api_mod.time, "sleep", lambda *_: None)
    # The retry recreates the session, so patch the factory to return the SAME fake
    # (which continues popping its script) — proving the retry actually re-issues.
    fake = _Session([requests.exceptions.ConnectionError("reset"), _Resp(200, {"ok": 1})])
    monkeypatch.setattr(api_mod.requests, "Session", lambda: fake)
    a = PlexAPI(logger=_Logger(), instance_config={"plex_token": "T"}, client_identifier="cid")
    assert a.get_identity() == {"ok": 1}
    assert fake.calls == 2


def test_watchlist_hits_discover_host_with_external_media():
    # The watchlist MUST target discover.provider.plex.tv (metadata.provider 404s) and
    # pass includeExternalMedia=1 (so each item carries the external Guid[] for id-resolve).
    captured = {}

    class _S(_Session):
        def request(self, **kwargs):
            captured["url"] = kwargs["url"]; captured["params"] = kwargs.get("params")
            return super().request(**kwargs)

    a = _api([_Resp(200, {"MediaContainer": {"Metadata": []}})])
    a._session = _S([_Resp(200, {"MediaContainer": {"Metadata": []}})])
    a.get_watchlist("USER-TOKEN")
    assert captured["url"] == "https://discover.provider.plex.tv/library/sections/watchlist/all"
    assert captured["params"] == {"includeExternalMedia": 1, "includeCollections": 1}


def test_local_list_endpoints_request_includeGuids():
    # Without includeGuids=1, modern plex:// items carry no external id → reconcile +
    # ratings + on_deck resolution are inert. Assert both list endpoints request it.
    captured = []

    class _S(_Session):
        def request(self, **kwargs):
            captured.append(kwargs.get("params")); return super().request(**kwargs)

    a = _api([])
    a._session = _S([_Resp(200, {"MediaContainer": {}}), _Resp(200, {"MediaContainer": {}})])
    a.get_section_all("1", plex_type=1)
    a.get_on_deck(token="t")
    assert captured[0].get("includeGuids") == 1 and captured[0].get("type") == 1
    assert captured[1].get("includeGuids") == 1


def test_watchlist_uses_per_user_token_and_pages():
    captured = {}

    class _S(_Session):
        def request(self, **kwargs):
            captured["token"] = kwargs["headers"]["X-Plex-Token"]
            captured["start"] = kwargs["headers"]["X-Plex-Container-Start"]
            return super().request(**kwargs)

    a = _api([_Resp(200, {"MediaContainer": {"Metadata": []}})])
    a._session = _S([_Resp(200, {"MediaContainer": {"Metadata": []}})])
    a.get_watchlist("USER-TOKEN", start=100, size=50)
    assert captured["token"] == "USER-TOKEN" and captured["start"] == "100"
