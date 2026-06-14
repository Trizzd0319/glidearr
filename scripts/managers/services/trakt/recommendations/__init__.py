from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class TraktRecommendationsManager(BaseManager, ComponentManagerMixin):
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

    # ── Recommendations ───────────────────────────────────────────────────

    def get_recommendations_shows(self, limit: int = 10) -> list:
        return self.global_cache.get_or_generate_cache(
            key=f"trakt/{self.user}/recommendations/shows",
            generator_function=lambda: self._fetch_shows(limit),
        ) if self.global_cache else self._fetch_shows(limit)

    def get_recommendations_movies(self, limit: int = 10) -> list:
        return self.global_cache.get_or_generate_cache(
            key=f"trakt/{self.user}/recommendations/movies",
            generator_function=lambda: self._fetch_movies(limit),
        ) if self.global_cache else self._fetch_movies(limit)

    def summarize_recommendations(self) -> dict:
        shows  = self.get_recommendations_shows()  or []
        movies = self.get_recommendations_movies() or []

        self.logger.log_info(f"[TraktRec] Recommended shows: {len(shows)}")
        for show in shows:
            self.logger.log_debug(f"  - {show.get('title')} ({show.get('year')})")

        self.logger.log_info(f"[TraktRec] Recommended movies: {len(movies)}")
        for movie in movies:
            self.logger.log_debug(f"  - {movie.get('title')} ({movie.get('year')})")

        return {"shows": shows, "movies": movies}

    # ── Private ───────────────────────────────────────────────────────────

    def _fetch_shows(self, limit: int) -> list:
        if not self.trakt_api:
            return []
        return self.trakt_api._make_request("recommendations/shows", params={"limit": limit}) or []

    def _fetch_movies(self, limit: int) -> list:
        if not self.trakt_api:
            return []
        return self.trakt_api._make_request("recommendations/movies", params={"limit": limit}) or []
