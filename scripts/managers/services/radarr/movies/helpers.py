import gzip
import json
from pathlib import Path

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrMoviesHelperManager(BaseManager, ComponentManagerMixin):
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

    @LoggerManager().log_function_entry
    @timeit("get_movie_by_tmdb")
    def get_movie_by_tmdb(self, instance, tmdb_id):
        """Look up a movie by TMDb ID (Radarr's primary external ID for movies)."""
        if not tmdb_id:
            self.logger.log_warning("TMDb ID is required to fetch a movie.")
            return None

        resolved_instance = self.instance_manager.resolve_instance(instance)
        results = self.radarr_api._make_request(
            resolved_instance, f"movie?tmdbId={tmdb_id}", fallback=[]
        ) or []
        if not results:
            self.logger.log_warning(f"No movies found in {resolved_instance} with TMDb ID {tmdb_id}")
            return None

        return results[0] if isinstance(results, list) else results

    @LoggerManager().log_function_entry
    @timeit("get_movie_by_tvdb")
    def get_movie_by_tvdb(self, instance, tvdb_id):
        """Deprecated — Radarr does not index by TVDb ID.  Use get_movie_by_tmdb instead."""
        self.logger.log_debug("get_movie_by_tvdb is deprecated; Radarr uses TMDb IDs. Use get_movie_by_tmdb.")
        return self.get_movie_by_tmdb(instance, tvdb_id)

    @LoggerManager().log_function_entry
    @timeit("get_movie_title_slug")
    def get_movie_title_slug(self, instance, movie_id):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        # Prefer going through retrieval sub-manager if available
        movies_mgr = self.registry.get("manager", "RadarrMoviesManager") if self.registry else None
        retrieval  = getattr(movies_mgr, "retrieval", None) if movies_mgr else None
        if retrieval and hasattr(retrieval, "get_movie_by_id"):
            movie_data = retrieval.get_movie_by_id(movie_id, resolved_instance)
        else:
            # Fallback: direct API call
            movie_data = self.radarr_api._make_request(
                resolved_instance, f"movie/{movie_id}", fallback=None
            ) if self.radarr_api else None
        if not movie_data:
            self.logger.log_warning(f"⚠️ No movie data found for ID {movie_id} in {resolved_instance}")
            return None
        return movie_data.get("titleSlug")

    @LoggerManager().log_function_entry
    @timeit("sanitize_movie_title")
    def sanitize_movie_title(self, title: str):
        return title.replace("’", "'").strip().lower()

    @LoggerManager().log_function_entry
    @timeit("extract_tmdb_id_from_movie")
    def extract_tmdb_id_from_movie(self, movie_obj: dict):
        """Return the TMDb ID from a Radarr movie dict (Radarr uses tmdbId, not tvdbId)."""
        return (
            movie_obj.get("tmdbId")
            or movie_obj.get("tmdb_id")
            or (movie_obj.get("externalIds") or {}).get("tmdb")
        )

    @LoggerManager().log_function_entry
    @timeit("extract_tvdb_id_from_movie")
    def extract_tvdb_id_from_movie(self, movie_obj: dict):
        """Deprecated — Radarr movies use TMDb IDs, not TVDb IDs.  Use extract_tmdb_id_from_movie instead."""
        self.logger.log_debug("extract_tvdb_id_from_movie is deprecated for Radarr; use extract_tmdb_id_from_movie")
        return self.extract_tmdb_id_from_movie(movie_obj)

    @LoggerManager().log_function_entry
    @timeit("get_movie_tags")
    def get_movie_tags(self, instance):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        return self.radarr_api._make_request(resolved_instance, "tags", fallback=[])

    @LoggerManager().log_function_entry
    @timeit("generate_movie_lookup_map")
    def generate_movie_lookup_map(self, instance):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        return self.radarr_api._make_request(resolved_instance, "movies", fallback=[])


class MoviesListHelper:
    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, api, logger):
        self.api = api
        self.logger = logger
        self._movie_cache = {}

    @LoggerManager().log_function_entry
    @timeit("bind_movie_list")
    def bind_movie_list(self, instance):
        @LoggerManager().log_function_entry
        @timeit("get_movie_list")
        def get_movie_list(force_refresh=False):
            if force_refresh or instance not in self._movie_cache:
                result = self.api._make_request(instance, "movies", fallback=[])
                if not isinstance(result, list):
                    self.logger.log_warning(
                        f"⚠️ Invalid /movies response from {instance} — got {type(result).__name__}, using []")
                    result = []
                self._movie_cache[instance] = result
            return self._movie_cache[instance]

        return get_movie_list

    def list_index(self, instance):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        index_path = Path("cache") / "radarr" / "library" / resolved_instance / "index.json.gz"
        if not index_path.exists():
            return []

        try:
            with gzip.open(index_path, "rt", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def search_index(self, instance, keyword):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        keyword = keyword.lower()
        index = self.list_index(resolved_instance)

        matches = [
            entry for entry in index
            if keyword in str(entry.get("title", "")).lower()
               or keyword in str(entry.get("path", "")).lower()
        ]
        return matches
