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
                # expiration_time is REQUIRED for regenerate_on_expiry to do anything.
                # get_or_generate_cache only sets `expired` inside `if expiration_time
                # is not None`, and the serve-from-disk short-circuit is
                # `not (expired and regenerate_on_expiry)` — so with no TTL the flag is
                # inert and an existing cache is served forever, generator never called.
                # Matches TraktHistoryManager's 24h. A None from the generator
                # (rate-limited) then serves the last-good copy instead of truncating.
                expiration_time=86_400,
                regenerate_on_expiry=True,
            ) or []
        return self._fetch_watchlist_shows() or []

    def get_watchlist_movies(self, force_refresh: bool = False) -> list:
        key = f"trakt/{self.user}/watchlist/movies"
        if force_refresh and self.global_cache:
            self.global_cache.invalidate_cache_key(key)
        if self.global_cache:
            return self.global_cache.get_or_generate_cache(
                key=key,
                generator_function=self._fetch_watchlist_movies,
                expiration_time=86_400,  # see get_watchlist_shows — TTL gates the flag
                regenerate_on_expiry=True,
            ) or []
        return self._fetch_watchlist_movies() or []

    # ── Private ───────────────────────────────────────────────────────────

    def _fetch_watchlist_shows(self):
        return self._fetch_watchlist("shows")

    def _fetch_watchlist_movies(self):
        return self._fetch_watchlist("movies")

    def _fetch_watchlist(self, kind: str):
        """Paginate the full watchlist with extended metadata.

        Mirrors ``TraktHistoryManager._fetch_full_movie_history``:

        * **Paginated.** The previous single-page ``limit=100`` call silently dropped
          everything past the first 100 entries — with several hundred movies
          watchlisted, most explicit intent never reached the acquisition pipeline
          and nothing logged that it had been truncated.
        * **Returns None (not []) on a failed page.** ``_make_request`` returns
          ``fallback`` (None) on an uncapped 429, so a rate-limited fetch must NOT
          cache an empty list over a good watchlist. The public getters coalesce
          None to [] so callers still receive a list.
        * **``extended=full``.** The default watchlist payload is
          ``{ids, title, year}`` only. Without this, ``rating`` and ``votes`` arrive
          empty, AcquisitionScorer drops both signals, and the weighted average
          divides by 0.75 instead of 1.00 — handing every watchlist item a floor of
          ``0.25 * 100 / 0.75 = 33.3`` regardless of merit.
        """
        if not self.trakt_api:
            return []
        self.trakt_api.rate_limited = False
        page, all_items = 1, []
        while True:
            items = self.trakt_api._make_request(
                f"users/me/watchlist/{kind}",
                params={"page": page, "limit": 100, "extended": "full"},
            )
            if items is None:
                # Request failed (likely rate-limited) — defer to cached watchlist
                # rather than caching a partial/empty result.
                self.logger.log_warning(
                    f"[TraktWatchlist] {kind} fetch interrupted (rate-limited) — "
                    "deferring to cached watchlist."
                )
                return None
            if not items:
                break
            all_items.extend(items)
            if len(items) < 100:
                break
            page += 1
        if all_items:
            self.logger.log_info(f"[TraktWatchlist] {len(all_items)} {kind} retrieved.")
        else:
            self.logger.log_warning(f"[TraktWatchlist] Empty {kind} watchlist.")
        return all_items
