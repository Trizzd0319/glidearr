from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrKeywordProcessorManager(BaseManager, ComponentManagerMixin):
    """
    Parses, filters, and transforms keyword metadata from Radarr movie entries
    into structured sets for analysis or ML enrichment.
    """

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "RadarrMoviesManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.dry_run          = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    def _resolve_instance(self, instance):
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    @LoggerManager().log_function_entry
    @timeit("get_keywords")
    def get_keywords(self, instance: str) -> dict:
        """
        Fetch all movies for the given instance and return a mapping of
        movie_id → normalized keywords list.
        """
        resolved = self._resolve_instance(instance)
        cached = self.global_cache.get(f"radarr.keywords.{resolved}", default=None)
        if cached is not None:
            return cached

        movies = self.radarr_api._make_request(resolved, "movie", fallback=[]) or []
        keyword_map = self.build_keywords_map(movies)
        self.global_cache.set(f"radarr.keywords.{resolved}", keyword_map)
        self.logger.log_info(f"Built keyword map for {len(keyword_map)} movies in {resolved}")
        return keyword_map

    @LoggerManager().log_function_entry
    @timeit("extract_keywords")
    def extract_keywords(self, movie_data: dict) -> list:
        """
        Extract and normalize keywords from a single Radarr movie entry.
        Radarr itself doesn't expose TMDb keywords directly, but some
        integrations inject them via the 'overview' or custom fields.
        We also extract genre-derived pseudo-keywords here.
        """
        keywords = movie_data.get("keywords", [])
        genres   = movie_data.get("genres", [])

        raw = list(keywords) + list(genres)
        normalized = sorted(set(self._normalize(k) for k in raw if isinstance(k, str) and k.strip()))
        return normalized

    @LoggerManager().log_function_entry
    @timeit("build_keywords_map")
    def build_keywords_map(self, movies: list) -> dict:
        """Returns a mapping of movie ID → normalized keywords list."""
        result = {}
        for movie in movies:
            kw = self.extract_keywords(movie)
            if kw:
                result[movie["id"]] = kw
        return result

    def _normalize(self, keyword: str) -> str:
        return keyword.lower().strip()
