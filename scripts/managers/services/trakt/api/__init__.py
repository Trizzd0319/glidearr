"""
TraktAPIManager
===============
Central HTTP layer for all Trakt TV API calls.

Manages:
- Bearer-token auth with proactive refresh (1 day before expiry)
- Rate limiting: 1 000 requests / 5-minute window
- Automatic retry on 401 (once) and 429 (honour Retry-After)
- Sub-manager instances: history, ratings, recommendations, watchlist, etc.
"""
from __future__ import annotations

import threading
import time

import requests

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager

_BASE_URL     = "https://api.trakt.tv"
_RATE_LIMIT   = 1_000
_RATE_WINDOW  = 300      # 5-minute window in seconds
_TOKEN_BUFFER = 86_400   # refresh 1 day before expiry
_MAX_429_WAIT = 30       # cap a single 429 Retry-After sleep (seconds). Beyond
                         # this we skip the live fetch and serve cached data so a
                         # long rate-limit window can't hang the whole run.


class TraktAPIManager(BaseManager, ComponentManagerMixin):
    """HTTP session + all Trakt TV sub-managers."""

    parent_name = "TraktManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = "TraktManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent       = kwargs.get("manager")
        # dry_run is resolved by BaseManager (explicit kwarg -> pre-super value ->
        # kwargs["manager"] -> registry parent -> False). The local resolution that used
        # to sit here defaulted to False, silently overwriting a parent-inherited True --
        # and self.dry_run feeds init_kwargs for ALL TEN sub-managers below, so it set the
        # mode for every Trakt write path at once. Second root-level instance in this
        # package after TraktManager's.

        # ── Auth / session ────────────────────────────────────────────────
        trakt_cfg             = (self.config.get("trakt", {}) if self.config else {})
        auth                  = trakt_cfg.get("authorization", {})
        self.client_id        = trakt_cfg.get("client_id", "")
        self.client_secret    = trakt_cfg.get("client_secret", "")
        self.access_token     = auth.get("access_token", "")
        self.refresh_token    = auth.get("refresh_token", "")
        self.token_expires_at = auth.get("created_at", 0) + auth.get("expires_in", 0)
        self.username         = trakt_cfg.get("username", "me")

        self._request_times: list[float] = []
        # Set whenever a 429 forces us to skip a live fetch. Generators check
        # this to return None (→ serve last-good cache) instead of empty data.
        self.rate_limited = False
        self._throttle_lock = threading.Lock()  # guards _request_times for threaded callers
        self._session = requests.Session()
        self._sync_session_headers()

        # ── Sub-managers ──────────────────────────────────────────────────
        # Lazy imports break the circular dependency
        # (sub-managers receive trakt_api=self to make HTTP calls)
        init_kwargs = dict(
            logger=self.logger,
            config=self.config,
            global_cache=self.global_cache,
            validator=self.validator,
            registry=self.registry,
            manager=self,
            dry_run=self.dry_run,
            trakt_api=self,
        )

        from scripts.managers.services.trakt.history         import TraktHistoryManager
        from scripts.managers.services.trakt.ratings         import TraktRatingsManager
        from scripts.managers.services.trakt.recommendations import TraktRecommendationsManager
        from scripts.managers.services.trakt.watchlist       import TraktWatchlistManager
        from scripts.managers.services.trakt.lookup          import TraktLookupManager
        from scripts.managers.services.trakt.progress        import TraktProgressManager

        sub_classes = {
            # ── called by TraktManager.run() ───────────────────────────────────
            "history":         TraktHistoryManager,
            "ratings":         TraktRatingsManager,
            "recommendations": TraktRecommendationsManager,
            "watchlist":       TraktWatchlistManager,
            "progress":        TraktProgressManager,
            # ── constructed every run, invoked by NOTHING ────────────────────────
            # A repo-wide search finds this only in its own class definition and in this
            # map -- no call site anywhere. Not a facade with a named caller (checklist Q9).
            #
            # KEPT, unlike its four former neighbours, because its methods are CORRECT:
            # 25 honest thin GET wrappers over real Trakt endpoints, no fabricated data,
            # no fail-open guards, no dead routes. Deleting wrong code and deleting merely
            # unused code are different decisions.
            #
            # ⚠️ One hazard worth knowing: TraktLookupManager.get_user_ratings(username)
            # and TraktRatingsManager.get_user_ratings() share a NAME with different
            # signatures on different managers -- exactly the checklist-Q10 trap ("does the
            # caller reach THAT method, or a similarly-named sibling?"). The LIVE one is on
            # `ratings`. See GLD-TRK-21.
            "lookup":          TraktLookupManager,
        }
        # trakt/lists REMOVED (session 94) -- dead, and _generate_unified_summary declares
        # five fields but writes three: `title` ("") and `in_library` (False) are set in the
        # defaultdict factory and never assigned, so both are constants rather than data. It
        # also returns `lists` as a set() (not JSON-serialisable) and, being a defaultdict,
        # silently includes every WATCHED series rather than only list members.
        # ⚠️ Unlike sync/universe/analytics this one holds UNIQUE capability worth keeping:
        # get_user_lists / get_list_items read the operator's OWN Trakt lists, which nothing
        # else in the repo does (the enrich daemon's `lists` scope is per-TITLE; acquisition
        # reads watchlist + recommendations only). "Acquire from my Trakt list X" is a real
        # gap it could fill -- so KEEP THE FILE, unconstructed. See GLD-TRK-20.
        # trakt/analytics REMOVED (session 94) — dead, and one method CANNOT WORK.
        # ``analyze_actors`` requests search/tvdb/{id}/people, which is not a Trakt route
        # (search/tvdb/{id} returns a SEARCH RESULT LIST; the real endpoint is
        # shows/{id}/people), so every call 404s -> fallback None -> swallowed by
        # `if not people: continue` -> returns {} for any history. Both methods are also
        # O(N) API calls per run (one request per watched series, a few hundred, against a
        # 1000/5min limit) to build a histogram that tautulli/users.compute_genre_affinity
        # and machine_learning/people_matrix already produce from data in hand.
        # See the module docstring and GLD-TRK-19; the file is dead and can be deleted.
        # trakt/universe REMOVED (session 94) — dead AND its data is fabricated.
        # ``get_universe_mapping`` returns hardcoded tvdb ids in which
        # marvel-cinematic-universe and arrowverse are THE SAME THREE IDS, and the block
        # runs in perfect sequential pairs across unrelated franchises (295759/60 MCU,
        # 295761/62 X-Men, 295763/64 DCEU …). That is generated placeholder data. Universe
        # membership feeds RETENTION (keep-universe = never deleted; bare universe =
        # deletable last resort), so a fabricated map would mis-protect and mis-expose.
        # The real machinery is plex/playlists/universe_order.py (curated + mdblist +
        # Kometa learning + the chronolists bake) and radarr/quality/universe_membership.py.
        # See the module docstring and GLD-TRK-18; the file is dead and can be deleted.
        # trakt/sync REMOVED from the map (session 94). It was constructed every run,
        # called by nothing, and every method it had is a LESS COMPLETE duplicate of
        # something that already works:
        #
        #   get_collection()      -> sync/collection/shows
        #        writeback/trakt_collection.py issues the SAME request itself, AND also
        #        does sync/collection/movies, which trakt/sync never had.
        #   get_watched()         -> sync/watched/shows        -> overlaps trakt/history
        #   get_watched_episodes()-> shows/{id}/progress/watched -> that IS trakt/progress
        #   last_watched_*        -> derived from trakt/history, and FAILS OPEN: every
        #        error path returns False = "not watched recently" = NOT protected.
        #
        # "sync" was Trakt's /sync/ API namespace, not write-synchronisation -- the module
        # is entirely GETs. The real write-back (collection + watched history + MAL) is
        # services/writeback, which runs in main.py's final phase and is config-gated.
        # The file itself is now dead; delete it (it cannot be removed from here).
        # NOT constructed here at all: trakt/shows/ (whose cache.py declares
        # parent_name = "TraktShowsManager") and trakt/movies/, which main.py builds
        # directly as TraktMoviesManager for RadarrOrchestrationManager.run_relational_pull.
        # movies therefore has a named caller; shows does not appear to be instantiated.

        # Sub-managers TraktManager.run() actually calls. A construction failure in one of
        # these is FATAL and must say so: the loop below otherwise sets the attribute to
        # None, and run() then dies on `'NoneType' object has no attribute
        # 'get_full_watch_history'` -- three frames from the real cause, with the actual
        # error text already discarded into a warning.
        #
        # A legitimate failure is REACHABLE here: TraktHistoryManager RAISES when it cannot
        # resolve dry_run from kwargs/parent/TraktManager/Main ("Refusing to initialize
        # without an explicit value"). Swallowing that turns a precise, actionable message
        # into an AttributeError about NoneType.
        #
        # The remaining one (`lookup`) is not called by run() at all, so its failure
        # genuinely costs nothing this run -- it keeps warn-and-continue.
        _REQUIRED = ("history", "ratings", "recommendations", "watchlist", "progress")

        for attr, cls in sub_classes.items():
            try:
                setattr(self, attr, cls(**init_kwargs))
            except Exception as exc:
                if attr in _REQUIRED:
                    raise RuntimeError(
                        f"[TraktAPI] required sub-manager '{attr}' failed to construct: "
                        f"{exc!r}. TraktManager.run() calls it directly, so continuing "
                        f"would fail later with an AttributeError on None instead of this."
                    ) from exc
                self.logger.log_warning(
                    f"[TraktAPI] Sub-manager '{attr}' failed to load: {exc} "
                    f"(not called by run(); continuing)")
                setattr(self, attr, None)

        self.logger.log_debug(
            f"[TraktAPI] Initialized "
            f"(configured={self._is_configured()}, "
            f"token_ok={not self._is_token_expiring()})"
        )

    # ── Auth helpers ──────────────────────────────────────────────────────

    def _is_configured(self) -> bool:
        return bool(self.client_id)

    def _is_token_expiring(self) -> bool:
        return self.token_expires_at > 0 and time.time() > self.token_expires_at - _TOKEN_BUFFER

    def _sync_session_headers(self):
        headers = {
            "Content-Type":      "application/json",
            "trakt-api-version": "2",
            "trakt-api-key":     self.client_id,
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        self._session.headers.update(headers)

    def _refresh_token(self) -> bool:
        if not all([self.refresh_token, self.client_id, self.client_secret]):
            self.logger.log_warning("[TraktAPI] Cannot refresh — missing credentials.")
            return False
        try:
            resp = requests.post(
                f"{_BASE_URL}/oauth/token",
                json={
                    "refresh_token": self.refresh_token,
                    "client_id":     self.client_id,
                    "client_secret": self.client_secret,
                    "redirect_uri":  "urn:ietf:wg:oauth:2.0:oob",
                    "grant_type":    "refresh_token",
                },
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            resp.raise_for_status()
            new_auth = resp.json()
            self.access_token     = new_auth["access_token"]
            self.refresh_token    = new_auth.get("refresh_token", self.refresh_token)
            self.token_expires_at = new_auth.get("created_at", 0) + new_auth.get("expires_in", 0)
            if self.config:
                trakt_cfg                  = self.config.get("trakt", {})
                trakt_cfg["authorization"] = new_auth
                self.config.set("trakt", trakt_cfg)
            self._sync_session_headers()
            self.logger.log_info("[TraktAPI] Access token refreshed and persisted.")
            return True
        except Exception as e:
            self.logger.log_warning(f"[TraktAPI] Token refresh failed: {e}")
            return False

    # ── Rate limiting ─────────────────────────────────────────────────────

    def _throttle(self):
        # Lock so concurrent (threaded) callers can't corrupt _request_times.
        # The actual sleep is held inside the lock intentionally: it serialises
        # the rare rate-limit wait so threads don't all blow past the window.
        with self._throttle_lock:
            now = time.time()
            self._request_times = [t for t in self._request_times if now - t < _RATE_WINDOW]
            if len(self._request_times) >= _RATE_LIMIT:
                wait = _RATE_WINDOW - (now - self._request_times[0]) + 0.1
                if wait > 0:
                    self.logger.log_debug(f"[TraktAPI] Rate limit — waiting {wait:.1f}s")
                    time.sleep(wait)
            self._request_times.append(time.time())

    # ── HTTP ──────────────────────────────────────────────────────────────

    def _make_request(self, endpoint: str, method: str = "GET", params=None,
                      data=None, fallback=None, _retry: bool = True):
        """Make an authenticated request to the Trakt API."""
        if not self._is_configured():
            return fallback
        if self._is_token_expiring():
            self.logger.log_info("[TraktAPI] Token expiring — refreshing before request.")
            self._refresh_token()
        self._throttle()

        url = f"{_BASE_URL}/{endpoint.lstrip('/')}"
        try:
            resp = self._session.request(
                method=method, url=url, params=params, json=data, timeout=15,
            )

            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 10))
                # Cap the wait so a long Retry-After can't stall the run, and
                # bound retry recursion to a single attempt. Plex/Tautulli is
                # the upstream source of truth for watch data, so a skipped live
                # Trakt fetch loses nothing the household actually did — the
                # caller serves its last-good cache instead (see the rate_limited
                # flag + GlobalCacheManager.get_or_generate_cache stale-serve).
                if wait > _MAX_429_WAIT or not _retry:
                    self.rate_limited = True
                    self.logger.log_warning(
                        f"[TraktAPI] 429 (Retry-After {wait}s) — skipping live "
                        f"fetch, serving cached data (cap {_MAX_429_WAIT}s)."
                    )
                    return fallback
                self.logger.log_warning(f"[TraktAPI] 429 — waiting {wait}s")
                time.sleep(wait)
                return self._make_request(endpoint, method, params, data, fallback, _retry=False)

            if resp.status_code == 401 and _retry:
                self.logger.log_warning("[TraktAPI] 401 — refreshing token and retrying once")
                if self._refresh_token():
                    return self._make_request(endpoint, method, params, data, fallback, _retry=False)
                return fallback

            if resp.status_code == 404:
                return fallback

            resp.raise_for_status()
            return resp.json() if resp.content else fallback

        except Exception as e:
            self.logger.log_debug(f"[TraktAPI] {method} /{endpoint} error: {e}")
            return fallback

    # ── Helpers ───────────────────────────────────────────────────────────

    def get_username(self) -> str:
        return self.username or "me"
