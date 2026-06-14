import gzip
import json
from pathlib import Path

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrMoviesRetrievalManager(BaseManager, ComponentManagerMixin):
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "RadarrMoviesManager"
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
    @timeit("get_all_movies")
    def get_all_movies(self, instance):
        resolved_instance = self._resolve_instance(instance)
        return self.radarr_api._make_request(resolved_instance, "movie", fallback=[])

    @LoggerManager().log_function_entry
    @timeit("get_movie_by_id")
    def get_movie_by_id(self, movie_id, instance):
        resolved_instance = self._resolve_instance(instance)
        return self.radarr_api._make_request(resolved_instance, f"movie/{movie_id}", fallback={})

    @LoggerManager().log_function_entry
    @timeit("get_movie_by_tmdb")
    def get_movie_by_tmdb(self, tmdb_id, instance):
        """Look up a movie by TMDb ID.  Falls back to full-library scan on miss."""
        resolved_instance = self._resolve_instance(instance)
        response = self.radarr_api._make_request(
            resolved_instance, f"movie?tmdbId={tmdb_id}", fallback=[]
        )
        if response:
            return response[0] if isinstance(response, list) else response

        self.logger.log_warning(
            f"Direct TMDb lookup failed for id={tmdb_id}. Falling back to full scan in {resolved_instance}."
        )
        all_movies = self.get_all_movies(resolved_instance)
        for movie in all_movies:
            if movie.get("tmdbId") == tmdb_id:
                return movie
        self.logger.log_info(f"Movie with TMDb ID {tmdb_id} not found in {resolved_instance}.")
        return None

    @LoggerManager().log_function_entry
    @timeit("get_all_movies_chunked")
    def get_all_movies_chunked(self, instance, chunk_size=200):
        """
        Radarr v3 /movie does not support server-side pagination.
        Fetches the full library and returns it in chunk_size slices for
        callers that expect a chunked iterator.
        """
        resolved_instance = self._resolve_instance(instance)
        all_movies = self.radarr_api._make_request(
            resolved_instance, "movie", fallback=[]
        ) or []
        self.logger.log_info(
            f"get_all_movies_chunked: fetched {len(all_movies)} movies from {resolved_instance}"
        )
        return all_movies

    @LoggerManager().log_function_entry
    @timeit("get_movie_history")
    def get_movie_history(self, instance):
        resolved_instance = self._resolve_instance(instance)
        return self.radarr_api._make_request(resolved_instance, "history", fallback=[])

    @LoggerManager().log_function_entry
    @timeit("get_library")
    def get_library(self, instance):
        """Return all valid movies for this instance, using global_cache when warm."""
        resolved_instance = self._resolve_instance(instance)
        cache_key = f"radarr.movies.{resolved_instance}.library"

        cached = self.global_cache.get(cache_key, default=None) if self.global_cache else None
        if cached is not None:
            valid = [m for m in cached if isinstance(m, dict) and "id" in m]
            self.logger.log_debug(
                f"get_library: returning {len(valid)} movies from cache for '{resolved_instance}'"
            )
            return valid

        movies = self._fetch_full_library(resolved_instance)
        valid_movies = [m for m in movies if isinstance(m, dict) and "id" in m]
        if self.global_cache:
            self.global_cache.set(cache_key, valid_movies)
        return valid_movies

    def _fetch_full_library(self, instance):
        return self.radarr_api._make_request(instance, "movie", fallback=[])

    def _fetch_movie_by_id(self, instance, movie_id):
        return self.radarr_api._make_request(instance, f"movie/{movie_id}")

    def get_metadata(self, instance):
        resolved_instance = self._resolve_instance(instance)
        return self.radarr_api._make_request(resolved_instance, "metadata") or []

    def get_movie_by_id_from_cache(self, instance, movie_id):
        resolved_instance = self._resolve_instance(instance)
        path = Path("cache") / "radarr" / "library" / resolved_instance / f"movie_{movie_id}.json.gz"
        if not path.exists():
            return None

        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
