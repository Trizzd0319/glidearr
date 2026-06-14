from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.machine_learning.sizing import size_model
from scripts.managers.machine_learning.sizing.file_comparison import classify_file_size
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrFileSizesManager(BaseManager, ComponentManagerMixin):
    """
    Estimates expected file sizes for Radarr movies based on quality definitions.
    """

    # Library-calibrated MiB/min table (shared single source of truth). Replaces
    # the former per-quality dict whose preferred=1999/max=2000 ceilings (mirrors
    # of the live Radarr quality-definition config) produced ~187 GB estimates
    # for 90-minute movies.
    QUALITY_MB_PER_MIN = size_model.CALIBRATED_MB_PER_MIN

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

        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    def _resolve_instance(self, instance):
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    def _get_profile_name(self, instance: str, profile_id: int) -> str:
        resolved = self._resolve_instance(instance)
        profiles = self.radarr_api._make_request(resolved, "qualityprofile", fallback=[]) or []
        for p in profiles:
            if p.get("id") == profile_id:
                return p.get("name", "Unknown")
        return "Unknown"

    @LoggerManager().log_function_entry
    @timeit("get_expected_file_size")
    def get_expected_file_size(self, instance: str, profile_id: int, runtime_minutes: int) -> float:
        """Estimate expected file size in bytes for a movie given its quality profile and runtime."""
        resolved = self._resolve_instance(instance)
        profile_name = self._get_profile_name(resolved, profile_id)
        mb_per_min = size_model.mb_per_min(profile_name)
        return mb_per_min * runtime_minutes * 1024 ** 2

    @LoggerManager().log_function_entry
    @timeit("get_median_file_size")
    def get_median_file_size(self, instance: str, profile_id: int) -> float:
        """
        Estimate file size from the median of all movies that share the same
        quality profile in this instance.  Falls back to the predefined estimate
        when no sample movies are available.
        """
        resolved = self._resolve_instance(instance)
        self.logger.log_info(f"Estimating median file size for profile {profile_id} in {resolved}")

        movies = self.radarr_api._make_request(resolved, "movie", fallback=[]) or []
        # Only consider movies that (a) match the profile and (b) have a real file
        sizes = []
        for m in movies:
            if m.get("qualityProfileId") == profile_id and m.get("hasFile") and m.get("sizeOnDisk", 0) > 0:
                sizes.append(m["sizeOnDisk"])

        if not sizes:
            self.logger.log_warning(f"No sample movies for profile {profile_id}. Using predefined estimate.")
            return self.get_predefined_file_size(resolved, profile_id, runtime_minutes=120)

        median = sorted(sizes)[len(sizes) // 2]
        self.logger.log_info(f"Median movie size for profile {profile_id}: {median / 1024**3:.2f} GB")
        return float(median)

    @LoggerManager().log_function_entry
    @timeit("get_predefined_file_size")
    def get_predefined_file_size(self, instance: str, profile_id: int, runtime_minutes: int = 120) -> float:
        """
        Return the expected file size in bytes using the predefined MB-per-minute table.
        Equivalent to get_expected_file_size but logs the profile name for auditing.
        """
        resolved = self._resolve_instance(instance)
        profile_name = self._get_profile_name(resolved, profile_id)

        mb_per_min = size_model.mb_per_min(profile_name)
        size = mb_per_min * runtime_minutes * 1024 ** 2
        self.logger.log_info(
            f"Predefined size for '{profile_name}' ({runtime_minutes} min) "
            f"@ {mb_per_min:.0f} MiB/min: {size / 1024**2:.1f} MB"
        )
        return size

    @LoggerManager().log_function_entry
    @timeit("compare_file_size")
    def compare_file_size(self, instance: str, movie_id: int, actual_bytes: float) -> str:
        """
        Compare actual file size against expected range.
        Returns 'upgrade', 'downgrade', or 'keep'.
        """
        resolved = self._resolve_instance(instance)
        movie = self.radarr_api._make_request(resolved, f"movie/{movie_id}", fallback=None)
        if not movie:
            return "keep"

        profile_id = movie.get("qualityProfileId", 0)
        runtime = movie.get("runtime", 90)
        expected = self.get_expected_file_size(resolved, profile_id, runtime)

        # Decision delegated to the brain (sizing/file_comparison); this method
        # keeps only the Radarr fetch + expected-size estimate (service-io).
        return classify_file_size(actual_bytes, expected)

    @LoggerManager().log_function_entry
    @timeit("generate_quality_flags")
    def generate_quality_flags(self, instance: str) -> dict:
        """Identify movies with quality or file issues."""
        resolved = self._resolve_instance(instance)
        movies = self.radarr_api._make_request(resolved, "movie", fallback=[]) or []

        flags: dict = {}
        for movie in movies:
            issues = []
            if movie.get("qualityCutoffNotMet", False):
                issues.append("quality_cutoff_not_met")
            if movie.get("monitored") and not movie.get("hasFile"):
                issues.append("missing_file")
            if issues:
                flags[movie["id"]] = {"title": movie.get("title"), "issues": issues}
        return flags
