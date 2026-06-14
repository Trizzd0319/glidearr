from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class TraktWatchlistManager(BaseManager, ComponentManagerMixin):
    parent_name = "TraktManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = "TraktManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent         = kwargs.get("manager")
        self.dry_run   = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)
        self.trakt_api = kwargs.get("trakt_api")

        trakt_cfg = (self.config.get("trakt", {}) if self.config else {})
        self.user = trakt_cfg.get("username", "default")

    # ── Watchlist ─────────────────────────────────────────────────────────

    def get_watchlist_shows(self, force_refresh: bool = False) -> list:
        key = f"trakt/{self.user}/watchlist/shows"
        if force_refresh and self.global_cache:
            self.global_cache.invalidate_cache_key(key)
        if self.global_cache:
            return self.global_cache.get_or_generate_cache(
                key=key,
                generator_function=self._fetch_watchlist_shows,
            )
        return self._fetch_watchlist_shows()

    def get_watchlist_movies(self, force_refresh: bool = False) -> list:
        key = f"trakt/{self.user}/watchlist/movies"
        if force_refresh and self.global_cache:
            self.global_cache.invalidate_cache_key(key)
        if self.global_cache:
            return self.global_cache.get_or_generate_cache(
                key=key,
                generator_function=self._fetch_watchlist_movies,
            )
        return self._fetch_watchlist_movies()

    # ── Private ───────────────────────────────────────────────────────────

    def _fetch_watchlist_shows(self) -> list:
        if not self.trakt_api:
            return []
        data = self.trakt_api._make_request(
            "users/me/watchlist/shows", params={"page": 1, "limit": 100}
        )
        if data:
            self.logger.log_info(f"[TraktWatchlist] {len(data)} shows retrieved.")
        else:
            self.logger.log_warning("[TraktWatchlist] Empty or failed show watchlist retrieval.")
        return data or []

    def _fetch_watchlist_movies(self) -> list:
        if not self.trakt_api:
            return []
        data = self.trakt_api._make_request(
            "users/me/watchlist/movies", params={"page": 1, "limit": 100}
        )
        if data:
            self.logger.log_info(f"[TraktWatchlist] {len(data)} movies retrieved.")
        else:
            self.logger.log_warning("[TraktWatchlist] Empty or failed movie watchlist retrieval.")
        return data or []
