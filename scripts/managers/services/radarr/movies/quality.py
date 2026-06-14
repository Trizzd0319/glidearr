from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrMoviesQualityManager(BaseManager, ComponentManagerMixin):
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
    @timeit("get_movie_profile_id")
    def get_movie_profile_id(self, instance, movie_id):
        """Retrieves the quality profile ID for a movie."""
        resolved_instance = self._resolve_instance(instance)
        movie_data = self._get_movie_data(resolved_instance, movie_id)
        return movie_data.get("qualityProfileId") if movie_data else None

    @LoggerManager().log_function_entry
    @timeit("update_movie_profile")
    def update_movie_profile(self, instance, movie_id, profile_id):
        """Updates the quality profile of a movie."""
        resolved_instance = self._resolve_instance(instance)
        movie_data = self._get_movie_data(resolved_instance, movie_id)
        if not movie_data:
            self.logger.log_warning(f"⚠️ Failed to fetch movie data for update in {resolved_instance}.")
            return False

        movie_data["qualityProfileId"] = profile_id
        response = self.radarr_api._make_request(resolved_instance, f"movie/{movie_id}", method="PUT", payload=movie_data)

        if response:
            self.logger.log_info(
                f"✅ Updated quality profile for movie {movie_id} in {resolved_instance} to {profile_id}.")
        else:
            self.logger.log_warning(f"❌ Failed to update quality profile for movie {movie_id} in {resolved_instance}.")
        return bool(response)

    @LoggerManager().log_function_entry
    @timeit("assign_default_profile_if_missing")
    def assign_default_profile_if_missing(self, movie_data: dict, instance: str):
        """Ensures a default profile is assigned if one is missing."""
        resolved_instance = self._resolve_instance(instance)
        if "qualityProfileId" not in movie_data or not movie_data["qualityProfileId"]:
            default_id = self.get_default_quality_profile(resolved_instance)
            self.logger.log_info(f"⚠️ No profile found. Assigning default: {default_id}")
            movie_data["qualityProfileId"] = default_id
        return movie_data

    @LoggerManager().log_function_entry
    @timeit("get_default_quality_profile")
    def get_default_quality_profile(self, instance):
        """Fetches the first quality profile as the default."""
        resolved_instance = self._resolve_instance(instance)
        profiles = self.radarr_api._make_request(resolved_instance, "qualityProfile") or []
        if not profiles:
            self.logger.log_warning(f"⚠️ No quality profiles found for {resolved_instance}.")
            return 1
        default_id = profiles[0].get("id", 1)
        self.logger.log_info(f"✅ Default profile ID for {resolved_instance} is {default_id}")
        return default_id

    @LoggerManager().log_function_entry
    @timeit("_get_movie_data")
    def _get_movie_data(self, instance, movie_id):
        """Helper method to retrieve movie data with error logging."""
        resolved_instance = self._resolve_instance(instance)
        if self.radarr_api is None:
            self.logger.log_warning(f"⚠️ radarr_api not available — cannot fetch movie {movie_id}")
            return None
        response = self.radarr_api._make_request(resolved_instance, f"movie/{movie_id}", fallback=None)
        if not response:
            self.logger.log_warning(f"⚠️ Failed to fetch movie with ID {movie_id} in {resolved_instance}.")
        return response
