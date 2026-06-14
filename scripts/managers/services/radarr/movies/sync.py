from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrMoviesSyncManager(BaseManager, ComponentManagerMixin):
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
    @timeit("add_movie")
    def add_movie(self, movie_payload, instance):
        resolved_instance = self._resolve_instance(instance)
        if self.dry_run:
            title = (movie_payload or {}).get("title", movie_payload)
            self.logger.log_info(f"[dry_run] Would add movie {title} to {resolved_instance}")
            return None
        return self.radarr_api._make_request(resolved_instance, "movie", method="POST", payload=movie_payload)

    @LoggerManager().log_function_entry
    @timeit("bulk_update_movies")
    def bulk_update_movies(self, instance, movie_updates):
        resolved_instance = self._resolve_instance(instance)
        if not movie_updates:
            self.logger.log_info(f"No movie updates provided for {resolved_instance}.")
            return False

        response = self.radarr_api._make_request(resolved_instance, "movie/editor", method="PUT", payload=movie_updates)
        if response:
            self.logger.log_info(f"Bulk update succeeded for {len(movie_updates)} movies in {resolved_instance}.")
        else:
            self.logger.log_warning(f"Bulk update failed for movies in {resolved_instance}.")
        return response

    @LoggerManager().log_function_entry
    @timeit("update_single_movie")
    def update_single_movie(self, movie_id, payload, instance):
        resolved_instance = self._resolve_instance(instance)
        return self.radarr_api._make_request(resolved_instance, f"movie/{movie_id}", method="PUT", payload=payload)

    @LoggerManager().log_function_entry
    @timeit("delete_movie")
    def delete_movie(self, movie_id, instance, delete_files=False):
        resolved_instance = self._resolve_instance(instance)
        if self.dry_run:
            self.logger.log_info(f"[dry_run] Would delete movie {movie_id} in {resolved_instance}")
            return None
        endpoint = f"movie/{movie_id}?deleteFiles={str(delete_files).lower()}"
        return self.radarr_api._make_request(resolved_instance, endpoint, method="DELETE")
