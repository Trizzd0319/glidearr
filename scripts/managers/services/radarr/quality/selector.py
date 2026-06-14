from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrQualitySelectorManager(BaseManager, ComponentManagerMixin):
    """
    Selects and validates quality profiles for Radarr movies.
    Resolves the best-fit profile based on instance, resolution tier,
    and custom-format scoring.
    """

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "RadarrQualityManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.dry_run          = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self._cached_profiles: dict = {}
        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    def _resolve_instance(self, instance):
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    # ── Profile fetching ─────────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("get_quality_profiles")
    def get_quality_profiles(self, instance: str) -> list:
        resolved = self._resolve_instance(instance)
        if resolved in self._cached_profiles:
            return self._cached_profiles[resolved]
        profiles = self.radarr_api._make_request(resolved, "qualityprofile", fallback=[]) or []
        self._cached_profiles[resolved] = profiles
        return profiles

    @LoggerManager().log_function_entry
    @timeit("get_default_quality_profile")
    def get_default_quality_profile(self, instance: str) -> int:
        resolved = self._resolve_instance(instance)
        profiles = self.get_quality_profiles(resolved)
        if not profiles:
            self.logger.log_warning(f"No quality profiles found for {resolved}")
            return 1
        default_id = profiles[0].get("id", 1)
        self.logger.log_info(f"Default profile ID for {resolved} is {default_id}")
        return default_id

    # ── Profile assignment ───────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("request_quality_change")
    def request_quality_change(self, movie_id: int, instance: str, profile_id: int) -> bool:
        """Apply a specific quality profile to a movie."""
        resolved = self._resolve_instance(instance)
        movie = self.radarr_api._make_request(resolved, f"movie/{movie_id}", fallback=None)
        if not movie:
            self.logger.log_warning(f"Movie {movie_id} not found in {resolved}")
            return False

        if self.dry_run:
            self.logger.log_info(f"[dry_run] Would set quality profile {profile_id} on movie {movie_id}")
            return True

        movie["qualityProfileId"] = profile_id
        result = self.radarr_api._make_request(resolved, f"movie/{movie_id}", method="PUT", payload=movie)
        if result:
            self.logger.log_info(f"Quality profile updated for movie {movie_id} → profile {profile_id}")
            return True
        self.logger.log_warning(f"Failed to update quality profile for movie {movie_id}")
        return False

    @LoggerManager().log_function_entry
    @timeit("assign_default_profile_if_missing")
    def assign_default_profile_if_missing(self, movie_data: dict, instance: str) -> dict:
        """Ensures a default profile is assigned if one is missing."""
        resolved = self._resolve_instance(instance)
        if not movie_data.get("qualityProfileId"):
            default_id = self.get_default_quality_profile(resolved)
            self.logger.log_info(f"No profile found. Assigning default: {default_id}")
            movie_data["qualityProfileId"] = default_id
        return movie_data

    # ── Profile validation ───────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("_is_valid_profile")
    def _is_valid_profile(self, profile_name: str, instance: str) -> bool:
        resolved = self._resolve_instance(instance)
        if not profile_name or not resolved:
            return False

        if self.config.get("ignore_resolution_check", False):
            self.logger.log_debug("Skipping resolution validation due to config override.")
            return True

        fallback_names = {"default", "unknown"}
        if profile_name.strip().lower() in fallback_names:
            self.logger.log_debug(f"Skipping fallback profile '{profile_name}' in {resolved}")
            return False

        if profile_name.strip().lower() == "any":
            if "4k" in resolved.lower() or "2160" in resolved:
                return True
            return False

        profiles = self.get_quality_profiles(resolved)
        target_res = "2160" if "4k" in resolved.lower() else "1080" if "1080" in resolved else "720"
        resolution_patterns = self.config.get("resolution_patterns", {
            "720": ["720p"],
            "1080": ["1080p"],
            "2160": ["2160p", "4k"],
        })

        valid_patterns = resolution_patterns.get(target_res, [])
        profile = next((p for p in profiles if p["name"].lower() == profile_name.lower()), None)
        if not profile:
            return False

        allowed_qualities = profile.get("items") or profile.get("qualities") or []
        for q in allowed_qualities:
            quality_name = (q.get("quality") or {}).get("name", "").lower()
            allowed = q.get("allowed", False)
            if allowed and any(pat.lower() in quality_name for pat in valid_patterns):
                return True

        return False

    # ── Best-profile selection ───────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("get_best_profile_for_instance")
    def get_best_profile_for_instance(self, instance: str) -> int:
        """
        Return the best quality profile ID for a given instance
        based on custom-format scores cached in global_cache.
        """
        resolved = self._resolve_instance(instance)
        profiles = self.get_quality_profiles(resolved)

        cf_scores = self.global_cache.get(f"radarr.quality.{resolved}", default={}) or {}
        # cf_scores expected shape: {profile_name: score}

        best_id = None
        best_score = float("-inf")
        for profile in profiles:
            name = profile.get("name", "")
            pid  = profile.get("id")
            if not self._is_valid_profile(name, resolved):
                continue
            score = cf_scores.get(name, 0) if isinstance(cf_scores, dict) else 0
            if score > best_score:
                best_score = score
                best_id = pid

        return best_id or self.get_default_quality_profile(resolved)
