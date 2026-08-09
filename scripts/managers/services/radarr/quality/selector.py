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
        # dry_run is resolved by BaseManager (explicit kwarg -> pre-super value ->
        # kwargs["manager"] -> registry parent -> False). The local resolution that
        # used to sit here defaulted to False, silently overwriting a
        # parent-inherited True — and request_quality_change() issues PUT movie/{id}.

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

    @staticmethod
    def _profile_cf_score(profile: dict) -> float:
        """A profile's custom-format quality bar, derived from the profile ITSELF.

        Preference order, most-explicit first:
          1. ``cutoffFormatScore`` — the profile's own statement of "a release scoring this
             is good enough"; the single number Radarr uses to stop upgrading.
          2. ``minFormatScore``    — the floor it will accept at all.
          3. the sum of POSITIVE ``formatItems`` scores — how much custom-format preference
             the profile expresses. Negative scores are exclusions, not preferences, so they
             are not netted off (a profile that bans three release groups is not thereby a
             *worse* profile than one that bans none).

        Returns 0.0 when a profile carries no custom-format configuration at all — which is
        a real answer ("this profile expresses no preference"), not a missing one.
        """
        for key in ("cutoffFormatScore", "minFormatScore"):
            v = profile.get(key)
            if isinstance(v, (int, float)) and v:
                return float(v)
        total = 0.0
        for item in (profile.get("formatItems") or []):
            if not isinstance(item, dict):
                continue
            try:
                s = float(item.get("score") or 0)
            except (TypeError, ValueError):
                continue
            if s > 0:
                total += s
        return total

    @LoggerManager().log_function_entry
    @timeit("get_best_profile_for_instance")
    def get_best_profile_for_instance(self, instance: str) -> int:
        """Return the best quality profile ID for an instance, by custom-format score.

        WAS READING A PHANTOM KEY. This used to score profiles from
        ``global_cache["radarr.quality.{instance}"]``, expecting ``{profile_name: score}``.
        That key does exist — but ``radarr/orchestration`` writes the raw LIST of profile
        dicts to it, not a score map. The guard ``score = cf_scores.get(name, 0) if
        isinstance(cf_scores, dict) else 0`` therefore returned **0 for every profile**, and
        because the comparison is strict (``score > best_score``) the FIRST valid profile won
        and every later tie was rejected. "Best by custom-format score" was really
        "first-valid-in-API-order", silently, on every call — and the isinstance guard is
        exactly what hid it.

        The score now comes from the profile itself (:meth:`_profile_cf_score`), so there is
        no second source to drift from and no key to be wrong about.
        """
        resolved = self._resolve_instance(instance)
        profiles = self.get_quality_profiles(resolved)

        best_id = None
        best_score = float("-inf")
        considered = 0
        scored = 0
        for profile in profiles:
            name = profile.get("name", "")
            pid = profile.get("id")
            if not self._is_valid_profile(name, resolved):
                continue
            considered += 1
            score = self._profile_cf_score(profile)
            if score:
                scored += 1
            # Strict > keeps the FIRST profile on a tie, which is deterministic given
            # Radarr returns profiles in a stable order. Stated because it is the tie-break
            # the old code fell back to for EVERY profile.
            if score > best_score:
                best_score = score
                best_id = pid

        if considered and not scored:
            # Not an error — but the caller asked for "best by custom format" and no
            # candidate expresses one, so say that the answer is really "first valid".
            self.logger.log_debug(
                f"[QualitySelector] {resolved}: none of the {considered} eligible profile(s) "
                f"carry a custom-format score — selection fell back to profile order.")
        elif best_id is not None:
            self.logger.log_debug(
                f"[QualitySelector] {resolved}: chose profile {best_id} "
                f"(cf score {best_score:g} of {considered} eligible).")

        return best_id or self.get_default_quality_profile(resolved)
