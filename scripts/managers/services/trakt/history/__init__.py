from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm

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
        return self.trakt_api._make_request(
            "sync/history",
            params={"page": page, "limit": limit, "type": "episode"},
        )

    def get_full_watch_history(self) -> list:
        page      = 1
        all_items = []
        self.logger.log_info("[TraktHistory] Fetching all episode history (paginated)...")

        while True:
            self.logger.log_debug(f"[TraktHistory] Fetching page {page}...")
            items = self.get_history(page=page, limit=100)
            if not items:
                break
            all_items.extend(items)
            if len(items) < 100:
                break
            page += 1
            # Rate limiting is handled centrally by TraktAPIManager._throttle();
            # the old fixed time.sleep(1) per page just added latency on top.

        self.logger.log_info(f"[TraktHistory] Retrieved {len(all_items)} total history items.")
        return all_items

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
        """Paginate through sync/history?type=movie and return the full list.

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
                "sync/history",
                params={"page": page, "limit": 100, "type": "movie"},
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

    def fetch_all_history_threaded(self, max_pages: int = 1_000, limit: int = 100) -> list:
        all_items: list = []

        def fetch_page(page_num):
            return self.trakt_api._make_request("sync/history", params={"page": page_num, "limit": limit})

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(fetch_page, i): i for i in range(1, max_pages + 1)}
            for future in tqdm(as_completed(futures), total=len(futures), desc="Fetching Trakt History"):
                items = future.result()
                if not items:
                    break
                all_items.extend(items)
                if len(items) < limit:
                    break

        self.logger.log_info(f"[TraktHistory] Retrieved {len(all_items)} items (threaded).")
        return all_items

    # ── Grouped History ───────────────────────────────────────────────────

    def get_history_grouped_by_series(self) -> dict:
        history = self.get_full_watch_history()
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

    def get_series_watch_counts(self) -> dict:
        counts: dict = defaultdict(int)
        for entry in self.get_full_watch_history():
            trakt_id = entry.get("trakt_id")
            if trakt_id:
                counts[trakt_id] += 1
        return dict(counts)
