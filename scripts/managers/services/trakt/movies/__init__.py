from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.services.trakt.movies.cache import TraktMovieCacheManager
from scripts.managers.services.trakt.movies.people import TraktMoviePeopleManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.managers.component_splitter import split_components


class TraktMoviesManager(BaseManager, ComponentManagerMixin):
    parent_name = "TraktMoviesManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = __class__.__name__
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent       = kwargs.get("manager")
        self.dry_run = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self.load_summary   = {}
        all_critical_loaded = True

        # Shared disk cache — injected into people so they share one instance
        self.cache = TraktMovieCacheManager(
            logger=self.logger, config=self.config,
            global_cache=self.global_cache, dry_run=self.dry_run,
        )

        init_kwargs = {
            "logger":        self.logger,
            "config":        self.config,
            "global_cache":  self.global_cache,
            "validator":     self.validator,
            "registry":      self.registry,
            "manager":       self,
            "dry_run":       self.dry_run,
            "cache_manager": self.cache,
        }

        all_component_classes = {
            "people": TraktMoviePeopleManager,
        }
        critical_keys = {"people"}

        critical_components, noncritical_components = split_components(
            all_components=all_component_classes,
            critical_keys=critical_keys,
            parent_name_match=self.parent_name,
            logger=self.logger,
            logger_context=self.__class__.__name__,
            init_kwargs=init_kwargs,
        )

        for name, cls in critical_components.items():
            try:
                instance = cls(**init_kwargs)
                setattr(self, name, instance)
                self.load_summary[name] = "✅ Loaded"
            except Exception as e:
                self.load_summary[name] = f"❌ Failed: {e}"
                all_critical_loaded = False

        self.all_components_loaded = all_critical_loaded
        self.log_filtered_component_summary(
            service_name="Trakt",
            component_label=self.__class__.__name__,
            critical_components=critical_components.keys(),
            noncritical_components=noncritical_components.keys(),
            all_critical_loaded=all_critical_loaded,
        )

    def enrich_movies(
        self,
        movies: list[dict],
        has_file_only: bool = True,
        watched_titles: set[str] | None = None,
        watched_tmdb_ids: set[int] | None = None,
        chunk_size: int = 500,
        cache_only: bool = False,
    ) -> list[dict]:
        """Proxy to TraktMoviePeopleManager.enrich_movies."""
        if not hasattr(self, "people") or self.people is None:
            self.logger.log_warning("[TraktMovies] people manager not available — returning movies unchanged")
            return movies
        return self.people.enrich_movies(
            movies,
            has_file_only=has_file_only,
            watched_titles=watched_titles,
            watched_tmdb_ids=watched_tmdb_ids,
            chunk_size=chunk_size,
            cache_only=cache_only,
        )

    def cache_stats(self) -> dict:
        return self.cache.stats() if self.cache else {}
