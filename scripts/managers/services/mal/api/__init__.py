"""
MALAPIManager — authenticated HTTP layer for the MyAnimeList API v2.
================================================================================
Mirrors the role of ``TraktAPIManager``: a single ``_make_request`` that attaches
the Bearer token, throttles, and refreshes the token once on a 401. Reads the
live token from config each call, so a refresh performed elsewhere is picked up.
Token refresh reuses the shared ``onboarding.oauth`` MAL helper.
"""
from __future__ import annotations

import time

import requests

from scripts.managers.factories.onboarding import oauth

_BASE = "https://api.myanimelist.net/v2"
_TIMEOUT = 15
_MIN_INTERVAL = 0.2  # gentle throttle between calls


class MALAPIManager:
    def __init__(self, config=None, logger=None):
        self.config = config
        self.logger = logger
        self._last_call = 0.0

    # ── credentials (read live so refreshes are reflected) ─────────────────────
    def _mal_cfg(self) -> dict:
        return (self.config.get("mal", {}) if self.config else {}) or {}

    def _token(self) -> str:
        return (self._mal_cfg().get("authorization") or {}).get("access_token", "") or ""

    def _throttle(self):
        delta = time.monotonic() - self._last_call
        if delta < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - delta)
        self._last_call = time.monotonic()

    def _refresh(self) -> bool:
        mal = self._mal_cfg()
        auth = mal.get("authorization", {})
        new = oauth.mal_refresh_token(
            mal.get("client_id", ""), mal.get("client_secret", ""),
            auth.get("refresh_token", ""), logger=self.logger,
        )
        if not new:
            return False
        mal["authorization"] = new
        if self.config:
            self.config.set("mal", mal)
        return True

    def _make_request(self, path: str, method: str = "GET", params=None, data=None,
                      fallback=None, _retry: bool = True):
        token = self._token()
        if not token:
            return fallback
        self._throttle()
        url = path if path.startswith("http") else f"{_BASE}/{path.lstrip('/')}"
        try:
            resp = requests.request(
                method=method, url=url,
                headers={"Authorization": f"Bearer {token}"},
                params=params, data=data, timeout=_TIMEOUT,
            )
            if resp.status_code == 401 and _retry and self._refresh():
                return self._make_request(path, method, params, data, fallback, _retry=False)
            if 200 <= resp.status_code < 300:
                return resp.json() if resp.content else {}
            self.logger and self.logger.log_warning(f"[MAL] {method} {path} → HTTP {resp.status_code}")
            return fallback
        except Exception as e:
            self.logger and self.logger.log_warning(f"[MAL] {method} {path} failed: {e}")
            return fallback

    def _paged(self, path: str, params: dict, max_pages: int = 10) -> list:
        out, page = [], 0
        while path and page < max_pages:
            resp = self._make_request(path, params=params if page == 0 else None, fallback={})
            if not isinstance(resp, dict):
                break
            out.extend(resp.get("data", []) or [])
            path = (resp.get("paging") or {}).get("next") or ""
            page += 1
        return out

    # ── reads ──────────────────────────────────────────────────────────────────
    _LIST_FIELDS = "list_status,num_episodes,genres,mean,media_type,start_season,alternative_titles"

    def get_anime_list(self, status: str = "", limit: int = 1000) -> list:
        params = {"fields": self._LIST_FIELDS, "limit": min(limit, 1000), "nsfw": "true"}
        if status:
            params["status"] = status
        return self._paged("users/@me/animelist", params)

    def get_suggestions(self, limit: int = 30) -> list:
        return self._paged("anime/suggestions", {"fields": self._LIST_FIELDS, "limit": min(limit, 100)}, max_pages=1)

    def get_seasonal(self, year: int, season: str, limit: int = 30) -> list:
        return self._paged(f"anime/season/{year}/{season}",
                           {"fields": self._LIST_FIELDS, "limit": min(limit, 100), "sort": "anime_num_list_users"},
                           max_pages=1)

    def get_anime(self, anime_id, fields: str = _LIST_FIELDS) -> dict:
        return self._make_request(f"anime/{anime_id}", params={"fields": fields}, fallback={}) or {}

    def search_anime(self, title: str, limit: int = 5) -> list:
        return self._paged("anime", {"q": title[:64], "fields": self._LIST_FIELDS, "limit": limit}, max_pages=1)

    # ── write ────────────────────────────────────────────────────────────────
    def update_list_status(self, anime_id, *, status=None, score=None, num_watched_episodes=None) -> dict:
        data = {}
        if status is not None:
            data["status"] = status
        if score is not None:
            data["score"] = int(score)
        if num_watched_episodes is not None:
            data["num_watched_episodes"] = int(num_watched_episodes)
        if not data:
            return {}
        return self._make_request(f"anime/{anime_id}/my_list_status", method="PATCH", data=data, fallback={}) or {}
