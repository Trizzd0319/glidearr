"""
plex/instances/api.py — Plex HTTP client (local PMS + plex.tv/Discover).
================================================================================
The canonical Plex HTTP handle. Named ``PlexAPI`` and exposed on a manager as
``plex_api`` (never a generic ``api``) to match the
``sonarr_api``/``radarr_api``/``tautulli_api``/``trakt_api`` convention.

Two transport tiers (DESIGN §6.3) with different hardening:

  * **LOCAL PMS** (``base_url``, ``X-Plex-Token``) — LAN, fast, officially-stable
    endpoints (/identity, /library/sections, /library/metadata/{rk}, /library/onDeck,
    /library/collections, /playlists, /status/sessions). Transient timeouts retried
    with a fresh session (mirrors TautulliAPI).
  * **EXTERNAL** (plex.tv account-v2 + ``metadata.provider.plex.tv`` Discover) — WAN,
    throttled, community-documented / UNSTABLE. Sliding-window throttle + HTTP-429
    Retry-After backoff capped at ~30s (mirrors Trakt's 429 discipline) so a long
    rate-limit window can never hang the run.

Security (DESIGN §6.2, post-incident posture):
  * TLS verification is ALWAYS on (these calls bear the highest-privilege token);
    we never pass ``verify=False``.
  * Every logged URL is scrubbed of its query string — ``X-Plex-Token`` is a URL
    param on Discover endpoints, so a bare ``response.url`` would leak it.
  * A STABLE ``X-Plex-Client-Identifier`` is required: v2 endpoints silently 401
    without it, and a per-run uuid4 (as the stress-test uses) spawns device churn
    / 2FA challenges. The identifier is resolved+persisted by PlexManager and
    passed in here.

All methods return parsed JSON (a dict) or the ``fallback`` (default None) — never
raise — so schema-tolerant callers can soft-empty on UNSTABLE drift.
"""
from __future__ import annotations

import threading
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

import requests

_PLEX_TV  = "https://plex.tv"
# The account watchlist + Discover metadata live on discover.provider.plex.tv. The
# old metadata.provider.plex.tv/library/sections/watchlist/all path is DEPRECATED and
# 404s ("Section 'watchlist' not found!") — using it silently returns an empty
# watchlist forever. Verified against python-plexapi (DISCOVER constant) + community
# reports (pd_zurg#98, Overseerr#4224).
_DISCOVER = "https://discover.provider.plex.tv"
_METADATA = "https://metadata.provider.plex.tv"   # legacy provider (kept for reference)


def build_base_url(url: str, port) -> str:
    """Local PMS base URL from a host (or full URL) + port."""
    url = (url or "localhost").strip().rstrip("/")
    port = str(port or "32400").strip()
    if url.startswith(("http://", "https://")):
        return url
    return f"http://{url}:{port}" if port else f"http://{url}"


def scrub_url(url: str) -> str:
    """Drop the query string from a URL before logging — ``X-Plex-Token`` rides
    on it for Discover endpoints. Best-effort; returns the input on parse error."""
    try:
        s = urlsplit(str(url))
        return urlunsplit((s.scheme, s.netloc, s.path, "", ""))
    except Exception:
        return str(url)


class PlexAPI:
    # Local-PMS transient-retry policy (mirrors TautulliAPI).
    _MAX_RETRIES     = 3
    _CONNECT_TIMEOUT = 5
    _READ_TIMEOUT    = 20
    _RETRY_DELAY     = 0.5

    # External (plex.tv / Discover) discipline (mirrors Trakt).
    _MAX_429_WAIT    = 30      # cap a single Retry-After sleep so a long window can't hang the run
    _EXT_RATE_LIMIT  = 90      # sliding-window budget for external calls …
    _EXT_RATE_WINDOW = 60      # … per this many seconds (gentle — plex.tv has no published limit)

    def __init__(self, logger, instance_config: dict, client_identifier: str | None = None, **kwargs):
        self.logger = logger
        cfg = instance_config or {}

        url  = cfg.get("url", "localhost")
        port = cfg.get("port", 32400)
        # The account-owner token. ``plex_token`` is the canonical flat-config key;
        # ``token`` accepted as an alias for test/back-compat.
        self.token    = cfg.get("plex_token") or cfg.get("token", "")
        self.base_url = (cfg.get("base_url") or build_base_url(url, port)).rstrip("/")

        # STABLE client identifier (resolved/persisted by PlexManager). Falling back
        # to a fresh uuid here keeps the client usable in tests, but production must
        # pass a persisted one (see PlexManager._ensure_client_identifier).
        self.client_identifier = (
            client_identifier or cfg.get("client_identifier") or str(uuid.uuid4())
        )
        self.product = "Glidearr"
        self.version = "1.0"

        # One keep-alive session across every call (connection pooling).
        self._session = requests.Session()

        # External-call throttle state.
        self._ext_lock  = threading.Lock()
        self._ext_times: list[float] = []

        # Observability — surfaced into plex/run_stats.
        self.calls_made    = 0
        self.ext_calls     = 0
        self.rate_limited  = False

    # ── headers ─────────────────────────────────────────────────────────────
    def _headers(self, token: str | None = None) -> dict:
        return {
            "X-Plex-Token":              token or self.token,
            "X-Plex-Client-Identifier":  self.client_identifier,
            "X-Plex-Product":            self.product,
            "X-Plex-Version":            self.version,
            "Accept":                    "application/json",
        }

    @property
    def configured(self) -> bool:
        return bool(self.token)

    # ── external throttle (sliding window) ──────────────────────────────────
    def _throttle_ext(self):
        with self._ext_lock:
            now = time.time()
            self._ext_times = [t for t in self._ext_times if now - t < self._EXT_RATE_WINDOW]
            if len(self._ext_times) >= self._EXT_RATE_LIMIT:
                wait = self._EXT_RATE_WINDOW - (now - self._ext_times[0]) + 0.1
                if wait > 0:
                    self.logger.log_debug(f"[PlexAPI] external rate limit — waiting {wait:.1f}s")
                    time.sleep(wait)
            self._ext_times.append(time.time())

    # ── core request ────────────────────────────────────────────────────────
    def _request(self, method: str, url: str, token: str | None = None,
                 params: dict | None = None, headers: dict | None = None,
                 external: bool = False, fallback=None, _retry: bool = True):
        """One Plex HTTP call. Returns parsed JSON (dict) or ``fallback``.

        TLS verification is left at the requests default (ON). The URL is scrubbed
        of its query string in every log line.
        """
        hdrs = self._headers(token)
        if headers:
            hdrs.update(headers)

        if external:
            self._throttle_ext()
            self.ext_calls += 1

        last_exc = None
        for attempt in range(self._MAX_RETRIES):
            try:
                self.calls_made += 1
                resp = self._session.request(
                    method=method, url=url, params=params, headers=hdrs,
                    timeout=(self._CONNECT_TIMEOUT, self._READ_TIMEOUT),
                )

                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 10) or 10)
                    if wait > self._MAX_429_WAIT or not _retry:
                        self.rate_limited = True
                        self.logger.log_warning(
                            f"[PlexAPI] 429 (Retry-After {wait}s) on {scrub_url(url)} — "
                            f"skipping (cap {self._MAX_429_WAIT}s)."
                        )
                        return fallback
                    self.logger.log_warning(f"[PlexAPI] 429 — waiting {wait}s ({scrub_url(url)})")
                    time.sleep(wait)
                    # external=False on the retry: the throttle slot + ext_calls were
                    # already charged on this entry, and the Retry-After sleep above
                    # already enforced the backoff — re-throttling would double-count.
                    return self._request(method, url, token, params, headers,
                                         external=False, fallback=fallback, _retry=False)

                if resp.status_code in (401, 403):
                    # Scope/token failure — caller decides how to degrade. Do NOT retry
                    # with broader scope (security: never fall through).
                    self.logger.log_debug(
                        f"[PlexAPI] {resp.status_code} on {scrub_url(url)} (token scope)."
                    )
                    return fallback
                if resp.status_code == 404:
                    return fallback

                resp.raise_for_status()
                if not resp.content:
                    return fallback
                try:
                    return resp.json()
                except ValueError:
                    self.logger.log_debug(f"[PlexAPI] non-JSON body from {scrub_url(url)}")
                    return fallback

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                # Transient — usually a stale pooled keep-alive. Recreate the session
                # so the retry opens a fresh connection (mirrors TautulliAPI).
                last_exc = e
                try:
                    self._session.close()
                except Exception:
                    pass
                self._session = requests.Session()
                if attempt < self._MAX_RETRIES - 1:
                    time.sleep(self._RETRY_DELAY * (attempt + 1))
            except Exception as e:
                self.logger.log_debug(f"[PlexAPI] {method} {scrub_url(url)} failed: {e}")
                return fallback

        self.logger.log_debug(
            f"[PlexAPI] {method} {scrub_url(url)} failed after {self._MAX_RETRIES} attempt(s): {last_exc}"
        )
        return fallback

    # ── LOCAL PMS (STABLE) ──────────────────────────────────────────────────
    def get_identity(self, fallback=None):
        """GET /identity — server identity + PMS version. The reachability probe."""
        return self._request("GET", f"{self.base_url}/identity", fallback=fallback)

    def get_sections(self, fallback=None):
        """GET /library/sections — library sections (movie/show/...)."""
        return self._request("GET", f"{self.base_url}/library/sections", fallback=fallback)

    def get_section_all(self, section_key, plex_type=None, start: int = 0,
                        size: int = 100, token: str | None = None, extra_params=None,
                        fallback=None):
        """GET /library/sections/{key}/all — paged. ``plex_type`` 1=movie, 2=show, 4=episode.
        ``token`` scopes to a specific member (per-user userRating reads).

        ``includeGuids=1`` is REQUIRED: Plex does NOT return the external Guid[] array
        on library LIST endpoints by default — only the internal ``plex://`` guid, which
        on a modern Plex Movie/TV agent carries no tmdb/tvdb. Without it the reconcile
        id-scan and ratings resolution are inert on every current server."""
        params = {"includeGuids": 1}
        if plex_type is not None:
            params["type"] = plex_type
        if extra_params:
            params.update(extra_params)
        headers = {"X-Plex-Container-Start": str(start), "X-Plex-Container-Size": str(size)}
        return self._request("GET", f"{self.base_url}/library/sections/{section_key}/all",
                             token=token, params=params or None, headers=headers, fallback=fallback)

    def get_pms_metadata(self, rating_key, fallback=None):
        """GET /library/metadata/{rk} — full item metadata from the local PMS."""
        return self._request("GET", f"{self.base_url}/library/metadata/{rating_key}", fallback=fallback)

    def get_on_deck(self, token: str | None = None, fallback=None):
        """GET /library/onDeck — continue-watching. With a per-user ``token`` (a
        minted account token works against the local PMS) this is that member's
        on-deck; without one it is the owner view. ``includeGuids=1`` so items carry
        external ids inline and resolve for free (no per-item Discover hop)."""
        return self._request("GET", f"{self.base_url}/library/onDeck", token=token,
                             params={"includeGuids": 1}, fallback=fallback)

    def get_collections(self, section_id=None, fallback=None):
        """GET /library/collections — manual + smart collections (optionally one section)."""
        params = {"sectionId": section_id} if section_id is not None else None
        return self._request("GET", f"{self.base_url}/library/collections",
                             params=params, fallback=fallback)

    def get_collection_children(self, rating_key, fallback=None):
        """GET /library/collections/{rk}/children — member items of a collection."""
        return self._request("GET", f"{self.base_url}/library/collections/{rating_key}/children",
                             fallback=fallback)

    def get_playlists(self, fallback=None):
        """GET /playlists — all playlists (we keep only video playlists)."""
        return self._request("GET", f"{self.base_url}/playlists", fallback=fallback)

    def get_playlist_items(self, rating_key, fallback=None):
        """GET /playlists/{rk}/items — playlist members."""
        return self._request("GET", f"{self.base_url}/playlists/{rating_key}/items", fallback=fallback)

    def get_sessions(self, fallback=None):
        """GET /status/sessions — current now-playing (1-call diagnostic stub only)."""
        return self._request("GET", f"{self.base_url}/status/sessions", fallback=fallback)

    # ── plex.tv ACCOUNT v2 (UNSTABLE) ───────────────────────────────────────
    def get_account(self, fallback=None):
        """GET plex.tv/api/v2/user — token-scope probe. The HARD gate (DESIGN §4.2)."""
        return self._request("GET", f"{_PLEX_TV}/api/v2/user", external=True, fallback=fallback)

    def get_home_users(self, fallback=None):
        """GET plex.tv/api/v2/home/users — enumerate Home/managed users (admin token)."""
        return self._request("GET", f"{_PLEX_TV}/api/v2/home/users", external=True, fallback=fallback)

    def switch_home_user(self, user_uuid, pin: str | None = None, fallback=None):
        """POST plex.tv/api/v2/home/users/{uuid}/switch — mint a per-user authToken.

        The admin token authorises the switch; ``pin`` (a CREDENTIAL — never logged)
        unlocks a PIN-protected profile. Returns the user object incl. ``authToken``.
        """
        params = {"pin": pin} if pin else None
        return self._request("POST", f"{_PLEX_TV}/api/v2/home/users/{user_uuid}/switch",
                             params=params, external=True, fallback=fallback)

    # ── Discover / metadata provider (UNSTABLE) ─────────────────────────────
    def get_watchlist(self, token: str, start: int = 0, size: int = 100, fallback=None):
        """GET discover.provider.plex.tv/library/sections/watchlist/all — a member's
        account watchlist (per-user ``token`` REQUIRED — this is account-level, not
        the local server). Paged via container headers.

        ``includeExternalMedia=1`` is what makes each item carry the external ``Guid[]``
        array (tmdb/imdb/tvdb); WITHOUT it (or with a restrictive ``includeFields``
        whitelist) the ids never resolve and the union can't de-dup against Trakt/MAL.
        Mirrors python-plexapi's watchlist params exactly."""
        headers = {"X-Plex-Container-Start": str(start), "X-Plex-Container-Size": str(size)}
        return self._request("GET", f"{_DISCOVER}/library/sections/watchlist/all",
                             token=token, params={"includeExternalMedia": 1, "includeCollections": 1},
                             headers=headers, external=True, fallback=fallback)

    def resolve_discover_metadata(self, rating_key, token: str | None = None, fallback=None):
        """GET discover.provider.plex.tv/library/metadata/{rk} — resolve a bare
        ``plex://`` Discover item to its external Guid[] (tmdb/tvdb/imdb). The
        ``includeExternalMedia=1`` param ensures the Guid[] array is present."""
        return self._request("GET", f"{_DISCOVER}/library/metadata/{rating_key}",
                             token=token, params={"includeExternalMedia": 1},
                             external=True, fallback=fallback)
