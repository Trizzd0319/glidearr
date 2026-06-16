"""
TraktMoviePeopleManager
========================
Fetches cast and crew for movies from Trakt's
GET /movies/{tmdb_id}/people endpoint.

Normalises the Trakt response into the credits format expected by
RadarrCacheRelationalManager.build_relations_from_movies:

    credits = {
        "cast": [{"name": str, "id": int|None, "character": str, "order": int}],
        "crew": [{"name": str, "id": int|None, "job": str, "department": str}],
    }

All disk caching is delegated to TraktMovieCacheManager.
Rate limiting and token refresh are handled internally.
"""
from __future__ import annotations

import time

import requests

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.machine_learning.acquisition.enrichment_prioritizer import (
    chunk_pool,
    chunk_window,
    enrich_action,
    priority_set,
    relevance_rank,
    relevance_window,
)
from scripts.managers.services.trakt.movies.cache import TraktMovieCacheManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager

_BASE_URL      = "https://api.trakt.tv"
_RATE_LIMIT    = 1000    # max calls per window
_RATE_WINDOW   = 300     # 5-minute window in seconds
_TOKEN_BUFFER  = 86_400  # refresh 1 day before expiry

# Trakt crew department key → display name used by relational manager
_DEPT_MAP: dict[str, str] = {
    "directing":         "Directing",
    "writing":           "Writing",
    "production":        "Production",
    "sound":             "Sound",
    "camera":            "Camera",
    "editing":           "Editing",
    "crew":              "Crew",
    "costume & make-up": "Costume & Make-Up",
    "visual effects":    "Visual Effects",
    "art":               "Art",
    "lighting":          "Lighting",
}


class TraktMoviePeopleManager(BaseManager, ComponentManagerMixin):

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "TraktMoviesManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent       = kwargs.get("manager")
        self.dry_run = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        # When the background enrichment daemon is enabled it owns ALL live Trakt
        # fetching. In that case this manager is GLOBALLY cache-only: get_people()
        # (and therefore every caller — relational enrichment, space-pressure,
        # repair, ratings) reads the daemon's cache and NEVER makes a live call,
        # so a main run can't hang on a 429. The daemon fills the cache out-of-band.
        self.cache_only = bool(
            ((self.config.get("daemons", {}) or {}).get("enrich") or {}).get("enabled")
        ) if self.config else False

        # Shared cache — injected by TraktMoviesManager, or created standalone
        self.cache: TraktMovieCacheManager = kwargs.get("cache_manager") or TraktMovieCacheManager(
            logger=self.logger, config=self.config,
            global_cache=self.global_cache, dry_run=self.dry_run,
        )

        # Read auth from config
        trakt_cfg             = (self.config.get("trakt", {}) if self.config else {})
        auth                  = trakt_cfg.get("authorization", {})
        self.client_id        = trakt_cfg.get("client_id", "")
        self.client_secret    = trakt_cfg.get("client_secret", "")
        self.access_token     = auth.get("access_token", "")
        self.refresh_token    = auth.get("refresh_token", "")
        self.token_expires_at = auth.get("created_at", 0) + auth.get("expires_in", 0)

        self._request_times: list[float] = []
        self._session = requests.Session()
        self._sync_session_headers()

        self.logger.log_debug(
            f"[TraktPeople] Initialized "
            f"(configured={self._is_configured()}, "
            f"token_ok={not self._is_token_expiring()})"
        )

    # ── Auth ──────────────────────────────────────────────────────────────────────

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
            self.logger.log_warning("[TraktPeople] Cannot refresh — missing refresh_token or credentials.")
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

            # Persist to config so next boot doesn't need to refresh again
            if self.config:
                trakt_cfg = self.config.get("trakt", {})
                trakt_cfg["authorization"] = new_auth
                self.config.set("trakt", trakt_cfg)

            self._sync_session_headers()
            self.logger.log_info("[TraktPeople] Access token refreshed and persisted.")
            return True
        except Exception as e:
            self.logger.log_warning(f"[TraktPeople] Token refresh failed: {e}")
            return False

    # ── Rate limiting ─────────────────────────────────────────────────────────────

    def _throttle(self):
        now = time.time()
        self._request_times = [t for t in self._request_times if now - t < _RATE_WINDOW]
        if len(self._request_times) >= _RATE_LIMIT:
            wait = _RATE_WINDOW - (now - self._request_times[0]) + 0.1
            if wait > 0:
                self.logger.log_debug(f"[TraktPeople] Rate limit — waiting {wait:.1f}s")
                time.sleep(wait)
        self._request_times.append(time.time())

    # ── HTTP ──────────────────────────────────────────────────────────────────────

    @timeit("_make_request")
    def _make_request(self, endpoint: str, fallback=None, _retry: bool = True):
        if not self._is_configured():
            return fallback
        if self._is_token_expiring():
            self.logger.log_info("[TraktPeople] Token expiring — refreshing before request.")
            self._refresh_token()
        self._throttle()

        url = f"{_BASE_URL}/{endpoint.lstrip('/')}"
        try:
            resp = self._session.get(url, timeout=30)

            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 10))
                self.logger.log_warning(f"[TraktPeople] 429 rate-limited — waiting {wait}s")
                time.sleep(wait)
                return self._make_request(endpoint, fallback, _retry=False)

            if resp.status_code == 401 and _retry:
                self.logger.log_warning("[TraktPeople] 401 — refreshing token and retrying once")
                if self._refresh_token():
                    return self._make_request(endpoint, fallback, _retry=False)
                return fallback

            if resp.status_code == 404:
                return fallback

            resp.raise_for_status()
            return resp.json() if resp.content else fallback

        except Exception as e:
            self.logger.log_debug(f"[TraktPeople] GET /{endpoint} error: {e}")
            return fallback

    # ── Normalisation ─────────────────────────────────────────────────────────────

    @staticmethod
    def _normalize(raw: dict) -> dict:
        """
        Convert Trakt /movies/{id}/people response to the credits dict shape
        expected by RadarrCacheRelationalManager.build_relations_from_movies.
        """
        cast: list[dict] = []
        for i, member in enumerate(raw.get("cast") or []):
            person     = member.get("person") or {}
            ids        = person.get("ids") or {}
            characters = member.get("characters") or []
            cast.append({
                "name":      person.get("name", ""),
                "id":        ids.get("tmdb"),
                "character": characters[0] if characters else "",
                "order":     i,
            })

        crew: list[dict] = []
        for dept_key, dept_members in (raw.get("crew") or {}).items():
            dept_name = _DEPT_MAP.get(dept_key.lower(), dept_key.title())
            for member in (dept_members or []):
                person = member.get("person") or {}
                ids    = person.get("ids") or {}
                for job in (member.get("jobs") or []):
                    crew.append({
                        "name":       person.get("name", ""),
                        "id":         ids.get("tmdb"),
                        "job":        job,
                        "department": dept_name,
                    })

        return {"cast": cast, "crew": crew}

    # ── Public ────────────────────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("get_people")
    def get_people(self, tmdb_id: int) -> dict | None:
        """
        Return normalised credits dict for tmdb_id.
        Checks disk cache first; fetches from Trakt API on miss.
        Returns None if Trakt is not configured or the movie has no credits.
        """
        cached = self.cache.get(tmdb_id)
        if cached is not None:
            return cached

        # Daemon owns live fetching — never hit the Trakt API from a run.
        if self.cache_only:
            return None

        raw = self._make_request(f"movies/{tmdb_id}/people")
        if raw is None:
            return None

        normalized = self._normalize(raw)
        self.cache.set(tmdb_id, normalized)
        return normalized

    def _advance_enrich_cursor(self, key: str, pool: list[int], size: int):
        """Read the round-robin cursor from global_cache, advance it one ``size``
        slice (pure ``chunk_window``), persist the new state, and return
        ``(start, end, ids)``. Skips the global_cache read/write entirely when the
        pool is empty — there is nothing to chunk and the old code never wrote a
        cursor for an empty pool, so this stays behaviour-identical."""
        if not pool:
            return 0, 0, set()
        last_id = -1
        if self.global_cache:
            raw = self.global_cache.get(key)
            if isinstance(raw, dict):
                last_id = raw.get("last_tmdb_id", -1)
        win = chunk_window(pool, last_id=last_id, size=size)
        if self.global_cache and win.cursor:
            self.global_cache.set(key, win.cursor)
        return win.start, win.end, win.ids

    @staticmethod
    def _relevance_row(m: dict) -> tuple:
        """(tmdbId, popularity, critic) for the relevance sort. popularity is Radarr's
        own metric; critic is the TMDb rating value, falling back to IMDb."""
        ratings = m.get("ratings") or {}
        crit = (ratings.get("tmdb") or {}).get("value")
        if crit is None:
            crit = (ratings.get("imdb") or {}).get("value")
        return (m["tmdbId"], m.get("popularity"), crit)

    def _advance_relevance_cursor(self, key: str, scored_rows: list, size: int):
        """Relevance round-robin for the owned pool: order rows by relevance, enrich
        the next ``size`` not-yet-done this cycle, persist the done-set. Returns
        ``(window_ids, done_count, total)``. Skips the global_cache read/write for an
        empty pool. The persisted cursor shape is ``{done_ids, size, total}`` — an old
        id-bisect cursor at this key (``last_tmdb_id`` only) is ignored, starting a
        fresh relevance cycle (a safe one-time transition)."""
        if not scored_rows:
            return set(), 0, 0
        ordered = relevance_rank(scored_rows)
        done_ids: set = set()
        if self.global_cache:
            raw = self.global_cache.get(key)
            if isinstance(raw, dict) and isinstance(raw.get("done_ids"), list):
                done_ids = set(raw["done_ids"])
        window, new_done = relevance_window(ordered, done_ids=done_ids, size=size)
        if self.global_cache:
            self.global_cache.set(key, {
                "done_ids": sorted(new_done), "size": size, "total": len(ordered),
            })
        return window, len(new_done), len(ordered)

    @LoggerManager().log_function_entry
    @timeit("enrich_movies")
    def enrich_movies(
        self,
        movies: list[dict],
        has_file_only: bool = False,
        watched_titles: set[str] | None = None,
        watched_tmdb_ids: set[int] | None = None,
        chunk_size: int = 500,
        unowned_chunk_size: int = 200,
        watched_chunk_size: int = 200,
        cache_only: bool = False,
    ) -> list[dict]:
        """
        Inject Trakt credits into each movie dict under the 'credits' key.

        cache_only — when True (the background enrichment daemon is handling all
        live fetching), attach credits ONLY from the on-disk cache and make ZERO
        live Trakt calls (not even for priority/watched movies). This guarantees a
        run can never block on a 429. Live fetching is delegated to enrich_daemon.py.

        Priority order each run:
        1. Movies watched on Plex (matched from Tautulli watch history by title).
           Cached ones attach immediately; the UNCACHED ones are budget-capped at
           *watched_chunk_size* live fetches per run and cursored, so importing a
           large watch history can't blast hundreds of live calls at once -> 429.
        2. Already-cached movies — free disk lookup, always injected.
        3. Remaining movies with files — processed in chunks of *chunk_size* per run.
           A cursor stored in global_cache advances each run so the full library
           cycles through without ever blasting 1,784 API calls at once.

        has_file_only       — if True, skip unowned entirely (default False now).
        watched_titles      — set of lowercase titles from Tautulli history.
        chunk_size          — max API fetches per run for owned non-priority movies.
        unowned_chunk_size  — max API fetches per run for unowned movies (200/run
                              = full cycle in ~3.5 weeks at 4 runs/day).
        watched_chunk_size  — max live fetches per run for uncached watched movies
                              (cached watched movies always attach, uncapped).
        """
        # ── Cache-only fast path (daemon owns all live fetching) ──────────────
        if cache_only or self.cache_only:
            enriched: list[dict] = []
            attached = missing = no_id = 0
            for movie in movies:
                tmdb_id = movie.get("tmdbId")
                if not tmdb_id:
                    enriched.append(movie)
                    no_id += 1
                    continue
                credits = self.cache.get(tmdb_id)   # fresh-only disk read; no network
                if credits:
                    enriched.append({**movie, "credits": credits})
                    attached += 1
                else:
                    enriched.append(movie)
                    missing += 1
            self.logger.log_table(
                ["Outcome", "Count"],
                [
                    ["total",       len(enriched)],
                    ["attached",    attached],
                    ["missing",     missing],
                    ["no_tmdb_id",  no_id],
                ],
                title="[TraktPeople] enrich_movies (cache_only, daemon owns live fetching)",
                caption="Cache-only enrichment pass attaching credits from disk only, no live Trakt calls.",
                descriptions=[
                    "movies processed this pass",
                    "movies that got credits from the disk cache",
                    "movies with no cached credits, left unenriched",
                    "movies skipped because they have no tmdbId",
                ],
            )
            return enriched

        watched_norm: set[str] = {t.lower().strip() for t in (watched_titles or [])}
        watched_ids:  set[int] = set(watched_tmdb_ids or [])

        # ── Selection (pure decision; brain owns it) ──────────────────────────
        all_with_tmdb      = [m for m in movies if m.get("tmdbId")]
        owned_candidates   = [m for m in all_with_tmdb if m.get("hasFile")]
        unowned_candidates = [] if has_file_only else [
            m for m in all_with_tmdb if not m.get("hasFile")
        ]

        priority_ids = priority_set(
            all_with_tmdb, watched_ids=watched_ids, watched_titles_norm=watched_norm
        )

        # Cache freshness for every candidate, read ONCE here and reused in the loop
        # (no second read). Drives both the watched-tier budget split and the per-row
        # cached/fetch decision.
        fresh: dict = {
            m["tmdbId"]: self.cache.get_fresh(m["tmdbId"]) for m in all_with_tmdb
        }
        fresh_ids = {tid for tid, (was_fresh, _) in fresh.items() if was_fresh}

        # Watched tier: cached watched rows always attach (free disk read); the
        # UNCACHED ones are budget-capped + cursored so a large watch-import can't
        # blast N live Trakt calls in one run and trip a 429.
        uncached_priority = sorted(priority_ids - fresh_ids)
        p_start, p_end, priority_fetch_ids = self._advance_enrich_cursor(
            "trakt/movie_people/watched_cursor", uncached_priority, watched_chunk_size
        )

        # Owned chunk: relevance round-robin (popularity/critic order), so the
        # titles that matter enrich sooner than tmdbId order would reach them.
        scored_owned = [
            self._relevance_row(m) for m in owned_candidates
            if m["tmdbId"] not in priority_ids
        ]
        chunk_ids, owned_done, owned_total = self._advance_relevance_cursor(
            "trakt/movie_people/chunk_cursor", scored_owned, chunk_size
        )
        # Unowned chunk: separate, slower id-cursor cadence (usually empty in prod —
        # has_file_only=True at the proxy). The global_cache get/set is service I/O.
        sorted_unowned = chunk_pool(unowned_candidates, exclude_ids=priority_ids)
        u_start, u_end, unowned_chunk_ids = self._advance_enrich_cursor(
            "trakt/movie_people/unowned_chunk_cursor", sorted_unowned, unowned_chunk_size
        )

        # ── Enrich ───────────────────────────────────────────────────────────
        enriched: list[dict] = []
        fetched    = 0
        cache_hit  = 0
        deferred   = 0
        no_file    = 0   # hasFile=False — intentionally skipped (has_file_only=True)
        no_tmdb_id = 0   # no tmdbId — cannot enrich

        for movie in movies:
            tmdb_id  = movie.get("tmdbId")
            has_file = movie.get("hasFile", False)

            if not tmdb_id:
                enriched.append(movie)
                no_tmdb_id += 1
                continue

            # Freshness was read once up front; reuse it (no second read).
            was_cached, cached = fresh.get(tmdb_id, (False, None))
            selected_for_fetch = (
                tmdb_id in priority_fetch_ids
                or (has_file     and tmdb_id in chunk_ids)
                or (not has_file and tmdb_id in unowned_chunk_ids)
            )
            action = enrich_action(
                has_file=has_file,
                is_priority=tmdb_id in priority_ids,
                already_cached=was_cached,
                selected_for_fetch=selected_for_fetch,
                has_file_only=has_file_only,
            )

            if action == "skip_no_file":
                enriched.append(movie)
                no_file += 1
                continue

            if action == "enrich":
                credits = cached if cached is not None else self.get_people(tmdb_id)
                if credits:
                    movie = {**movie, "credits": credits}
                    if was_cached:
                        cache_hit += 1
                    else:
                        fetched += 1
            else:
                deferred += 1

            enriched.append(movie)

        self.logger.log_table(
            ["Outcome", "Count"],
            [
                ["total",       len(enriched)],
                ["priority",    len(priority_ids)],
                ["fetched",     fetched],
                ["cache_hit",   cache_hit],
                ["deferred",    deferred],
                ["watched",     f"{p_start}->{p_end}/{len(uncached_priority)}"],
                ["owned",       f"{owned_done}/{owned_total}"],
                ["unowned",     f"{u_start}->{u_end}/{len(sorted_unowned)}"],
                ["no_tmdb_id",  no_tmdb_id],
            ],
            title="[TraktPeople] enrich_movies",
            caption="Per-run credit enrichment outcomes plus the watched/owned/unowned cursor progress.",
            descriptions=[
                "movies processed this pass",
                "watched movies prioritized (trakt ids plus tautulli titles)",
                "movies credits were live-fetched from Trakt for",
                "movies credits came from the disk cache for",
                "movies deferred to a later run by the cursor budget",
                "uncached watched cursor window start to end over total",
                "owned relevance cursor done count over total this cycle",
                "unowned cursor window start to end over total",
                "movies skipped because they have no tmdbId",
            ],
        )
        return enriched
