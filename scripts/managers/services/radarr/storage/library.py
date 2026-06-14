from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.config.cache_keys import CacheKeyPaths as Paths
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrLibraryCacheManager(BaseManager, ComponentManagerMixin):
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "RadarrStorageManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.manager          = parent
        self.dry_run          = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    def _resolve_instance(self, instance):
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    @LoggerManager().log_function_entry
    @timeit("get_movies_cache")
    def get_movies_cache(self, instance: str) -> dict:
        resolved_instance = self._resolve_instance(instance)
        key = f"{Paths.radarr.SONARR_LIBRARY}.{resolved_instance}"
        data = self.global_cache.load_cache(key) or {}
        self.logger.log_debug(f"📦 Loaded movie cache for {resolved_instance}: {len(data)} entries")
        return data

    @LoggerManager().log_function_entry
    @timeit("get_movie_by_tmdb")
    def get_movie_by_tmdb(self, tmdb_id: int, instance: str) -> dict | None:
        library = self.get_movies_cache(instance)
        for movie in library.values():
            if str(movie.get("tmdbId")) == str(tmdb_id):
                self.logger.log_debug(f"✅ Found movie with TMDB ID {tmdb_id} in {instance}")
                return movie
        self.logger.log_debug(f"❌ Movie with TMDB ID {tmdb_id} not found in {instance}")
        return None

    @LoggerManager().log_function_entry
    @timeit("get_movie_by_title")
    def get_movie_by_title(self, title: str, instance: str) -> dict | None:
        library = self.get_movies_cache(instance)
        for movie in library.values():
            if movie.get("title", "").lower() == title.lower():
                self.logger.log_debug(f"✅ Found movie with title '{title}' in {instance}")
                return movie
        self.logger.log_debug(f"❌ Movie with title '{title}' not found in {instance}")
        return None

    @LoggerManager().log_function_entry
    @timeit("is_movie_in_library")
    def is_movie_in_library(self, tmdb_id: int, instance: str) -> bool:
        exists = self.get_movie_by_tmdb(tmdb_id, instance) is not None
        self.logger.log_debug(f"📍 Movie TMDB ID {tmdb_id} present in {instance}: {exists}")
        return exists

    @staticmethod
    @LoggerManager().log_function_entry
    @timeit("warm_cache")
    def warm_cache(logger, cache, instance=None):
        key = f"{Paths.radarr.SONARR_LIBRARY}.{instance or 'default'}"
        data = cache.get(key)
        if data:
            logger.log_debug(f"📦 Warmed cache key: {key} ({len(data)} entries)")
        else:
            logger.log_warning(f"⚠️ Cache key {key} is empty or missing")
