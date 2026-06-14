import time

import requests


class TautulliAPI:
    """
    Thin HTTP wrapper for the Tautulli JSON API (v2).

    All methods return the raw response dict (or None on error).
    Higher-level managers are responsible for extracting
    response["response"]["data"] and caching results.
    """

    # Transient-failure retry policy. Timeouts/connection errors on a pooled
    # keep-alive GET are usually a stale connection (the server closed an idle
    # keep-alive); urllib3 does NOT auto-retry read timeouts, so we retry here
    # and recreate the session so the retry uses a fresh connection.
    _MAX_RETRIES     = 3
    _CONNECT_TIMEOUT = 5      # seconds — fast-fail if the server is unreachable
    _READ_TIMEOUT    = 15     # seconds — allow for genuinely slow responses
    _RETRY_DELAY     = 0.5    # seconds, ×attempt

    def __init__(self, logger, instance_config: dict, cache=None, **kwargs):
        self.logger = logger
        self.cache  = cache

        url       = instance_config.get("url", "localhost")
        port      = str(instance_config.get("port", "8181"))
        self.api_key  = instance_config.get("api") or instance_config.get("api_key", "")
        self.base_url = instance_config.get("base_url", f"http://{url}:{port}").rstrip("/")

        # Shared session → connection pooling / keep-alive. Avoids a fresh
        # TCP+TLS handshake on every call (matters most for the thousands of
        # per-rating_key get_metadata calls during a cold metadata-index build).
        self._session = requests.Session()

    # ── Core ──────────────────────────────────────────────────────────────

    def _request(self, cmd: str, params: dict | None = None):
        payload = {"apikey": self.api_key, "cmd": cmd}
        if params:
            payload.update({k: v for k, v in params.items() if v is not None})

        last_exc = None
        for attempt in range(self._MAX_RETRIES):
            try:
                resp = self._session.get(
                    f"{self.base_url}/api/v2", params=payload,
                    timeout=(self._CONNECT_TIMEOUT, self._READ_TIMEOUT),
                )
                resp.raise_for_status()
                return resp.json()
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                # Transient — most often a stale pooled keep-alive connection.
                # Recreate the session so the retry opens a fresh connection.
                last_exc = e
                try:
                    self._session.close()
                except Exception:
                    pass
                self._session = requests.Session()
                if attempt < self._MAX_RETRIES - 1:
                    time.sleep(self._RETRY_DELAY * (attempt + 1))
            except Exception as e:
                # Non-transient (HTTP status, bad JSON, …) — do not retry.
                self.logger.log_error(f"[TautulliAPI] {cmd} failed: {e}")
                return None

        self.logger.log_warning(
            f"[TautulliAPI] {cmd} failed after {self._MAX_RETRIES} attempt(s): {last_exc}"
        )
        return None

    # ── Server ────────────────────────────────────────────────────────────

    def validate(self):
        return self._request("get_server_info")

    def get_server_info(self):
        return self._request("get_server_info")

    def get_activity(self):
        """Current active streams."""
        return self._request("get_activity")

    def get_recently_added(self, count: int = 50, media_type: str = None):
        return self._request("get_recently_added", {"count": count, "media_type": media_type})

    # ── Users ─────────────────────────────────────────────────────────────

    def get_users(self):
        return self._request("get_users")

    def get_user(self, user_id):
        return self._request("get_user", {"user_id": user_id})

    def get_user_watch_time_stats(self, user_id=None, query_days: str = "1,7,30,0"):
        return self._request("get_user_watch_time_stats", {
            "user_id":    user_id,
            "query_days": query_days,
        })

    def get_user_player_stats(self, user_id):
        return self._request("get_user_player_stats", {"user_id": user_id})

    def get_user_logins(self, user_id=None):
        return self._request("get_user_logins", {"user_id": user_id})

    # ── History ───────────────────────────────────────────────────────────

    def get_history(self, length: int = 1000, start: int = 0, user_id=None,
                    section_id=None, media_type: str = None, search: str = None):
        return self._request("get_history", {
            "length":     length,
            "start":      start,
            "user_id":    user_id,
            "section_id": section_id,
            "media_type": media_type,
            "search":     search,
        })

    # ── Libraries ─────────────────────────────────────────────────────────

    def get_libraries(self):
        return self._request("get_libraries")

    def get_library_names(self):
        return self._request("get_library_names")

    def get_library(self, section_id, include_last_accessed: bool = False):
        return self._request("get_library", {
            "section_id":            section_id,
            "include_last_accessed": str(include_last_accessed).lower(),
        })

    def get_library_user_stats(self, section_id, grouping: int = 0):
        return self._request("get_library_user_stats", {
            "section_id": section_id,
            "grouping":   grouping,
        })

    def get_library_watch_time_stats(self, section_id, grouping: int = 0,
                                     query_days: str = "1,7,30,0"):
        return self._request("get_library_watch_time_stats", {
            "section_id": section_id,
            "grouping":   grouping,
            "query_days": query_days,
        })

    def get_library_media_info(self, section_id=None, rating_key=None, **kwargs):
        params = {}
        if section_id:
            params["section_id"] = section_id
        if rating_key:
            params["rating_key"] = rating_key
        params.update(kwargs)
        return self._request("get_library_media_info", params)

    def get_libraries_table(self, **kwargs):
        return self._request("get_libraries_table", kwargs or None)

    # ── Metadata ──────────────────────────────────────────────────────────

    def get_metadata(self, rating_key):
        return self._request("get_metadata", {"rating_key": rating_key})

    # ── Play statistics ───────────────────────────────────────────────────

    def get_home_stats(self, time_range: int = 30, stats_type: str = "plays",
                       stats_count: int = 10):
        return self._request("get_home_stats", {
            "time_range":  time_range,
            "stats_type":  stats_type,
            "stats_count": stats_count,
        })

    def get_plays_by_date(self, time_range: int = 30, user_id=None,
                          y_axis: str = "plays"):
        return self._request("get_plays_by_date", {
            "time_range": time_range,
            "user_id":    user_id,
            "y_axis":     y_axis,
        })

    def get_plays_by_hourofday(self, time_range: int = 30, user_id=None,
                               y_axis: str = "plays"):
        return self._request("get_plays_by_hourofday", {
            "time_range": time_range,
            "user_id":    user_id,
            "y_axis":     y_axis,
        })

    def get_plays_by_dayofweek(self, time_range: int = 30, user_id=None,
                               y_axis: str = "plays"):
        return self._request("get_plays_by_dayofweek", {
            "time_range": time_range,
            "user_id":    user_id,
            "y_axis":     y_axis,
        })

    def get_plays_by_top_10_platforms(self, time_range: int = 30, user_id=None,
                                      y_axis: str = "plays"):
        return self._request("get_plays_by_top_10_platforms", {
            "time_range": time_range,
            "user_id":    user_id,
            "y_axis":     y_axis,
        })

    def get_plays_by_top_10_users(self, time_range: int = 30, y_axis: str = "plays"):
        return self._request("get_plays_by_top_10_users", {
            "time_range": time_range,
            "y_axis":     y_axis,
        })

    def get_plays_per_month(self, time_range: int = 12, y_axis: str = "plays",
                            user_id=None):
        return self._request("get_plays_per_month", {
            "time_range": time_range,
            "y_axis":     y_axis,
            "user_id":    user_id,
        })

    def get_stream_type_by_top_10_platforms(self, time_range: int = 30):
        return self._request("get_stream_type_by_top_10_platforms", {
            "time_range": time_range,
        })

    def get_stream_type_by_top_10_users(self, time_range: int = 30):
        return self._request("get_stream_type_by_top_10_users", {
            "time_range": time_range,
        })
