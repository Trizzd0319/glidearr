from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class TraktHistoryManager(BaseManager, ComponentManagerMixin):
    parent_name = "TraktManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = "TraktManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent          = kwargs.get("manager")
        self.trakt_api  = kwargs.get("trakt_api")

        # Resolve dry_run — walk the chain: kwargs → parent → TraktManager → Main.
        # Never default to False; raise if unresolvable.
        _dry_run = kwargs.get("dry_run")
        if _dry_run is None:
            _dry_run = getattr(parent, "dry_run", None) if parent else None
        if _dry_run is None and self.registry:
            try:
                _trakt = self.registry.get("manager", "TraktManager")
                _dry_run = getattr(_trakt, "dry_run", None) if _trakt else None
            except Exception:
                pass
        if _dry_run is None and self.registry:
            try:
                _main = self.registry.get("manager", "Main")
                _dry_run = getattr(_main, "dry_run", None) if _main else None
            except Exception:
                pass
        if _dry_run is None:
            raise ValueError(
                f"❌ {self.__class__.__name__} could not resolve dry_run from kwargs, "
                f"TraktManager, or Main. Refusing to initialize without an explicit value "
                f"from config.json to prevent accidental destructive operations."
            )
        self.dry_run = bool(_dry_run)

    # ── Basic History ─────────────────────────────────────────────────────

    def get_history(self, page: int = 1, limit: int = 1_000):
        if not self.trakt_api:
            return None
        # Trakt takes the media type as a PATH segment with a PLURAL name
        # (sync/history/episodes). ``type=episode`` as a QUERY param is not a
        # recognised parameter - it was silently dropped, so this returned the
        # UNFILTERED history and the movie fetch below got the identical payload.
        return self.trakt_api._make_request(
            "sync/history/episodes",
            params={"page": page, "limit": limit},
        )

    def get_full_watch_history(self):
        """Paginate through sync/history/episodes and return the full list.

        Returns None (not []) if a page request fails — the SAME contract as
        ``_fetch_full_movie_history`` and ``fetch_all_history_threaded`` below.
        This method was the odd one out: it treated a failed request and the end
        of the data as the same event (``if not items: break``), so a rate-limited
        or dropped page returned a TRUNCATED history presented as complete — and
        ``get_history`` returns None when ``trakt_api`` is absent, so a missing
        API read as "watched nothing" too.

        That matters because this feed reaches the watched-set that guards
        deletion: ``radarr/repair/anomaly`` folds Trakt history into
        ``watched_tmdb_ids``, which is the hard guard
        (``if keep_policy or tmdb_id in watched_tmdb_ids: guarded``) that stops a
        WATCHED movie being pruned. A short history silently un-guards every title
        missing from it.
        """
        page      = 1
        all_items = []
        self.logger.log_info("[TraktHistory] Fetching all episode history (paginated)...")

        while True:
            self.logger.log_debug(f"[TraktHistory] Fetching page {page}...")
            items = self.get_history(page=page, limit=100)
            if items is None:
                # Request failed (rate-limited, dropped, or no trakt_api) — NOT the
                # end of the data. Returning what we have would present a partial
                # history as complete.
                self.logger.log_warning(
                    f"[TraktHistory] episode history fetch interrupted at page {page} "
                    f"(after {len(all_items)} items) — returning None so callers do not "
                    f"mistake a truncated history for a complete one."
                )
                return None
            if not items:
                break                      # genuine end of data (empty page)
            all_items.extend(items)
            if len(items) < 100:
                break                      # short page = end of data
            page += 1
            # Rate limiting is handled centrally by TraktAPIManager._throttle();
            # the old fixed time.sleep(1) per page just added latency on top.

        self.logger.log_info(f"[TraktHistory] Retrieved {len(all_items)} total history items.")
        return all_items

    def get_full_watch_history_cached(self) -> list:
        """Return all watched EPISODES from Trakt sync history, cached for 24 hours.

        THE MISSING TWIN. ``get_full_movie_history_cached`` has existed all along; the
        episode side had no cached form, so every caller re-paginated the ENTIRE episode
        history live — ~1,900 rows at 100/page against a rate-limited API. This module's
        own ``history_dataframe`` docstring names the asymmetry: *"the movie side is
        served by the 24h get_full_movie_history_cached, the episode side re-paginates
        live on every call. Pass ``rows`` if you are calling this in a loop."* Telling
        callers to work around a missing cache is the tell that the cache should exist.

        Worse, ``TraktManager.run()`` called the uncached form and **discarded the
        result** — a full paginated sweep every run for nothing, because nothing wrote it
        anywhere. That call now warms this key instead, so the sweep it was already
        paying for is the one the consumers read.

        Same contract as the movie twin: the generator returns None on a failed page, so
        ``get_or_generate_cache`` serves the last-good copy rather than caching a
        truncated history. That matters here — this feed reaches ``watched_tmdb_ids``,
        the guard that stops a watched title being pruned (see
        ``get_full_watch_history``'s docstring).

        Key ``trakt/history/episodes`` mirrors ``trakt/history/movies``, so a consumer
        can read either directly from global_cache the way
        ``radarr/orchestration.run_relational_pull`` already reads the movie half.
        """
        if not self.global_cache:
            return self.get_full_watch_history()
        return self.global_cache.get_or_generate_cache(
            key="trakt/history/episodes",
            generator_function=self.get_full_watch_history,
            expiration_time=86_400,
            # Watched-set source — must refresh daily so newly-watched episodes register.
            # On a rate-limited fetch the generator returns None and the last-good copy is
            # served (no hang, no empty cache).
            regenerate_on_expiry=True,
        )

    def get_full_movie_history_cached(self) -> list:
        """Return all watched movies from Trakt sync history, cached for 24 hours."""
        if not self.global_cache:
            return self._fetch_full_movie_history()
        return self.global_cache.get_or_generate_cache(
            key="trakt/history/movies",
            generator_function=self._fetch_full_movie_history,
            expiration_time=86_400,
            # Watched-set source — must actually refresh daily so newly-watched
            # movies register. On a rate-limited fetch the generator returns
            # None and the last-good copy is served (no hang, no empty cache).
            regenerate_on_expiry=True,
        )

    def _fetch_full_movie_history(self):
        """Paginate through sync/history/movies and return the full list.

        Returns None (not []) if a page request fails — e.g. the call was
        rate-limited and skipped — so the cache layer serves the last-good
        history instead of caching a truncated/empty list. Plex/Tautulli
        corroborates this data (and is pushed to Trakt), so a stale Trakt
        copy loses nothing the household actually watched.
        """
        if not self.trakt_api:
            return []
        self.trakt_api.rate_limited = False
        page      = 1
        all_items = []
        self.logger.log_info("[TraktHistory] Fetching all movie history (paginated)...")
        while True:
            items = self.trakt_api._make_request(
                "sync/history/movies",
                params={"page": page, "limit": 100},
            )
            if items is None:
                # Request failed (likely rate-limited) — defer to cached history
                # rather than caching a partial/empty result.
                self.logger.log_warning(
                    "[TraktHistory] movie history fetch interrupted "
                    "(rate-limited) — deferring to cached history."
                )
                return None
            if not items:
                break
            all_items.extend(items)
            if len(items) < 100:
                break
            page += 1
            # Rate limiting handled by TraktAPIManager._throttle(); fixed sleep removed.
        self.logger.log_info(f"[TraktHistory] Retrieved {len(all_items)} movie history items.")
        return all_items

    def get_latest_episodes_by_series(self, episodes: list) -> dict:
        latest: dict = {}
        for item in episodes:
            show    = item.get("show") or {}
            episode = item.get("episode") or {}
            tvdb_id = (show.get("ids") or {}).get("tvdb")
            if not tvdb_id or not episode:
                continue
            if tvdb_id not in latest or episode.get("watched_at", "") > latest[tvdb_id].get("watched_at", ""):
                latest[tvdb_id] = episode
        return latest

    # ── Threaded Fetch ────────────────────────────────────────────────────

    def fetch_all_history_threaded(self, max_pages: int = 1_000, limit: int = 100,
                                   kind: str = "episodes", workers: int = 5):
        """Paginated history fetch - ``workers`` pages in flight, rows in PAGE ORDER.

        Returns the full list, or ``None`` if a page request failed (rate-limited),
        so a caller can serve its last-good cache instead of a partial list - the
        same contract as ``_fetch_full_movie_history``.

        ``kind`` is the Trakt PATH segment (``episodes`` / ``movies``); pass None
        for the unfiltered feed.

        REWRITTEN. The previous version submitted EVERY page up front
        (``range(1, max_pages + 1)``), and neither of its ``break`` statements
        cancelled a queued future - ``ThreadPoolExecutor.__exit__`` calls
        ``shutdown(wait=True)``, so all 1000 requests were issued regardless of
        when it stopped reading. ``as_completed`` also yields in COMPLETION order,
        not page order, so the short-page stop test fired on whichever page
        returned first - usually an EMPTY page past the end of history - which
        truncated the result to near-nothing while the rest of the requests kept
        hammering Trakt. It could not return a valid array.
        """
        endpoint = f"sync/history/{kind}" if kind else "sync/history"

        def fetch_page(page_num):
            return self.trakt_api._make_request(
                endpoint, params={"page": page_num, "limit": limit}
            )

        all_items: list = []
        page = 1
        done = False
        with ThreadPoolExecutor(max_workers=workers) as executor:
            while not done and page <= max_pages:
                wave = list(range(page, min(page + workers, max_pages + 1)))
                # Submit ONE wave and wait for it. executor.map yields results in
                # INPUT order, so pages concatenate correctly, concurrency stays
                # bounded at `workers`, and nothing is left queued when we stop.
                for pnum, items in zip(wave, list(executor.map(fetch_page, wave))):
                    if items is None:
                        self.logger.log_warning(
                            f"[TraktHistory] threaded fetch interrupted at page "
                            f"{pnum} (rate-limited) - returning None so the caller "
                            f"keeps its last-good cache."
                        )
                        return None
                    if not items:
                        done = True
                        break
                    all_items.extend(items)
                    if len(items) < limit:
                        done = True
                        break
                page += len(wave)

        self.logger.log_info(
            f"[TraktHistory] Retrieved {len(all_items)} {kind or 'history'} "
            f"item(s) (threaded)."
        )
        return all_items

    # ── Grouped History ───────────────────────────────────────────────────

    def get_history_grouped_by_series(self) -> dict:
        history = self.get_full_watch_history_cached()
        if history is None:
            # Fetch failed. Returning {} here is indistinguishable from "this
            # household has watched no episodes", so say so rather than letting a
            # transient failure read as an empty history. (Propagating None would
            # be better still, but this method's callers have not been audited —
            # the warning at least makes the degradation visible.)
            self.logger.log_warning(
                "[TraktHistory] grouped-by-series: history fetch FAILED — returning an "
                "empty map, which downstream reads as 'no episodes watched'."
            )
            return {}
        grouped: dict = defaultdict(list)

        for item in history:
            show    = item.get("show")
            episode = item.get("episode")
            if not show or not episode:
                continue
            tvdb_id = (show.get("ids") or {}).get("tvdb")
            if tvdb_id:
                grouped[tvdb_id].append(episode)

        return dict(grouped)

    def get_series_watch_counts(self, id_source: str = "trakt") -> dict:
        """``{series_id: episode_play_count}`` across the user's episode history.

        History rows carry NO top-level ``trakt_id`` - a show's ids live at
        ``entry["show"]["ids"]``. The previous lookup read that absent top-level
        key, so this returned ``{}`` on EVERY call no matter how large the
        history was (0 of 1913 rows carried it).

        ``id_source`` selects which id keys the map: ``"trakt"`` (default) or
        ``"tvdb"`` to match ``get_history_grouped_by_series`` and Sonarr's own
        series keying.
        """
        counts: dict = defaultdict(int)
        history = self.get_full_watch_history_cached()
        if history is None:
            # See get_history_grouped_by_series — an empty count map is read as
            # "never watched", which is the opposite of "we could not tell".
            self.logger.log_warning(
                "[TraktHistory] series watch counts: history fetch FAILED — returning an "
                "empty map, which downstream reads as zero plays for every series."
            )
            return {}
        for entry in history:
            if not entry.get("episode"):
                continue                       # movie rows carry no show/episode
            sid = ((entry.get("show") or {}).get("ids") or {}).get(id_source)
            if sid:
                counts[sid] += 1
        return dict(counts)

    # ── DataFrame projection ────────────────────────────────────────────

    # Stable flat schema. Movies and episodes SHARE it: for a movie row the bare
    # id columns describe the film and every show_* column is null; for an episode
    # they describe the episode and the show_* columns its parent. ``title`` is
    # deliberately the SHOW title on episode rows - it is the useful grouping key -
    # with the episode's own name kept in ``episode_title``.
    HISTORY_DF_COLUMNS = (
        "play_id", "watched_at", "watched_date", "days_since", "action", "type",
        "title", "year", "season", "episode", "episode_title",
        "tmdb_id", "tvdb_id", "imdb_id", "trakt_id",
        "show_title", "show_tmdb_id", "show_tvdb_id", "show_imdb_id", "show_trakt_id",
    )

    _DF_NUMERIC = (
        "play_id", "year", "season", "episode", "days_since",
        "tmdb_id", "tvdb_id", "trakt_id",
        "show_tmdb_id", "show_tvdb_id", "show_trakt_id",
    )

    @staticmethod
    def _project_history_row(entry: dict) -> dict:
        """One raw Trakt history row -> one flat dict keyed by HISTORY_DF_COLUMNS."""
        kind = entry.get("type")
        # "movie" -> entry["movie"], "episode" -> entry["episode"]
        item = entry.get(kind) or {}
        show = entry.get("show") or {}
        iids = item.get("ids") or {}
        sids = show.get("ids") or {}
        return {
            "play_id":       entry.get("id"),
            "watched_at":    entry.get("watched_at"),
            "action":        entry.get("action"),
            "type":          kind,
            "title":         show.get("title") or item.get("title"),
            "year":          show.get("year") or item.get("year"),
            "season":        item.get("season"),
            "episode":       item.get("number"),
            "episode_title": item.get("title") if kind == "episode" else None,
            "tmdb_id":       iids.get("tmdb"),
            "tvdb_id":       iids.get("tvdb"),
            "imdb_id":       iids.get("imdb"),
            "trakt_id":      iids.get("trakt"),
            "show_title":    show.get("title"),
            "show_tmdb_id":  sids.get("tmdb"),
            "show_tvdb_id":  sids.get("tvdb"),
            "show_imdb_id":  sids.get("imdb"),
            "show_trakt_id": sids.get("trakt"),
        }

    def history_dataframe(self, kind: str = "episodes", *, rows: list | None = None):
        """Flat, filterable DataFrame of the Trakt watch history.

        ``kind``  "episodes" | "movies" | "all" - which feed to project.
        ``rows``  project a caller-supplied raw list instead of fetching. Use this
                  for one-off analysis, tests, or to avoid a second network call
                  when the caller already holds the history.

        NOTE both feeds are now served by a 24h cache
        (``get_full_watch_history_cached`` / ``get_full_movie_history_cached``), so
        calling this in a loop no longer re-paginates. ``rows`` is still the way to
        project a caller-supplied list without touching the cache at all.

        Always returns the FULL column set - an empty history yields an empty
        frame with typed columns, so a caller can filter without a KeyError.

        Typical use::

            df = mgr.history_dataframe("movies")
            df[df.days_since <= 90]                       # recent window
            df.groupby("tmdb_id").size()                  # plays per film
            df[df.title.str.contains("Tangled", na=False)]
        """
        import pandas as pd

        if rows is None:
            rows = []
            if kind in ("episodes", "all"):
                rows += list(self.get_full_watch_history_cached() or [])
            if kind in ("movies", "all"):
                rows += list(self.get_full_movie_history_cached() or [])

        df = pd.DataFrame(
            [self._project_history_row(r) for r in (rows or [])],
            columns=list(self.HISTORY_DF_COLUMNS),
        )
        df["watched_at"] = pd.to_datetime(df["watched_at"], utc=True, errors="coerce")
        df["watched_date"] = df["watched_at"].dt.date
        df["days_since"] = (pd.Timestamp.now(tz="UTC") - df["watched_at"]).dt.days
        for col in self._DF_NUMERIC:
            # Int64 (nullable) - a missing tvdb on a movie row must stay null
            # rather than silently becoming 0 and colliding with a real id.
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
        return df.sort_values("watched_at", ascending=False).reset_index(drop=True)

    def save_history_parquet(self, path, kind: str = "all", *, rows: list | None = None):
        """Write :meth:`history_dataframe` to ``path`` as Parquet; returns the frame.

        ``watched_date`` holds ``datetime.date`` objects, which Parquet cannot
        infer from an object column, so it is cast to string on the way out. The
        full timestamp survives in ``watched_at``.
        """
        df = self.history_dataframe(kind, rows=rows)
        out = df.copy()
        out["watched_date"] = out["watched_date"].astype("string")
        out.to_parquet(path, index=False)
        self.logger.log_info(
            f"[TraktHistory] wrote {len(out)} {kind} history row(s) -> {path}"
        )
        return df
