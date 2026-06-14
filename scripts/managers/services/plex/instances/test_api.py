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
