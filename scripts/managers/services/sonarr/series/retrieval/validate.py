from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrSeriesRetrievalValidationManager(BaseManager, ComponentManagerMixin):
    """
    Validates Sonarr series library integrity, schema consistency, and tag correctness.
    """

    def __init__(self, logger=None, config=None, global_cache=None, cache_manager=None, validator=None, registry=None, **kwargs):
        self.parent_name = self.__class__.__name__.replace("Manager", "")
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        # 🔧 Dual cache setup
        manager = kwargs.get("manager") or {}
        self.manager = manager
        self.sonarr_cache = cache_manager or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)

        self.sonarr_api = kwargs.get("sonarr_api") or getattr(manager, "sonarr_api", None)
        self.logger = self.logger or getattr(manager, "logger", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(manager, "instance_manager", None)

        # 🔗 Letter-bucketed series cache — prefer sonarr_cache.series (SonarrCacheSeriesManager),
        # fall back to the retrieval-layer series_cache if the cache layer isn't wired yet.
        self.series_cache = (
            getattr(self.sonarr_cache, "series", None) or
            getattr(manager, "series_cache", None)
        )

        if not self.logger:
            raise ValueError("❌ SonarrSeriesRetrievalValidationManager requires a logger")

        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("validate_series_count")
    def validate_series_count(self, instance: str, live_series: list = None) -> float:
        """
        Checks for drift between live Sonarr series and cache count.

        ``live_series``: when the caller already fetched the live ``/series`` list THIS run (the
        ``run_series_retrieval`` after a live refresh — exactly when this drift check runs), pass it
        in to skip a redundant second full ``/series`` fetch of all ~8k series. The drift comparison
        (live count vs cache count) is identical; we just don't pull the same list twice.
        """
        resolved_instance = self.instance_manager.resolve_instance(instance)
        if live_series is None:
            live_series = self.sonarr_api.get_all_sonarr_apis()[resolved_instance].all_series()
        cached_ids = self.series_cache.get_all_series_ids(resolved_instance)

        live_count = len(live_series)
        cache_count = len(cached_ids)
        diff_pct = abs(live_count - cache_count) / max(live_count, 1)

        if diff_pct > 0.10:
            self.logger.log_warning(
                f"⚠️ Library validation: live={live_count}, cached={cache_count}, difference={diff_pct:.2%} exceeds threshold."
            )
        else:
            self.logger.log_info(
                f"✅ Library validation passed: live={live_count}, cached={cache_count}, diff={diff_pct:.2%}"
            )
        return diff_pct

    @LoggerManager().log_function_entry
    @timeit("validate_series_schema")
    def validate_series_schema(self, instance: str, required_fields: list = None) -> list:
        """
        Ensures all cached series have the required fields.
        """
        required_fields = required_fields or ["id", "title", "path", "qualityProfileId"]
        resolved_instance = self.instance_manager.resolve_instance(instance)
        errors = []

        for letter in "abcdefghijklmnopqrstuvwxyz0123456789_":
            for series in self.series_cache.load_letter_cache(resolved_instance, letter):
                missing = [f for f in required_fields if not series.get(f)]
                if missing:
                    errors.append({"id": series.get("id"), "title": series.get("title"), "missing": missing})

        if errors:
            self.logger.log_warning(f"🛠️ {len(errors)} series entries have missing required fields:")
            for e in errors[:10]:
                self.logger.log_info(f"   → ID: {e['id']}, Title: {e['title']}, Missing: {e['missing']}")
        else:
            self.logger.log_info("✅ All cached series passed schema validation.")
        return errors

    @LoggerManager().log_function_entry
    @timeit("validate_series_tags")
    def validate_series_tags(self, instance: str) -> list:
        """
        Ensures all tag references used by cached series exist in the known tag list.
        """
        tag_key = f"sonarr/{instance}/tags.json"
        tag_data = self.sonarr_cache.get(tag_key) or []
        known_tags = {t["id"] for t in tag_data if isinstance(t, dict)}

        resolved_instance = self.instance_manager.resolve_instance(instance)
        invalid_usages = []

        for letter in "abcdefghijklmnopqrstuvwxyz0123456789_":
            for series in self.series_cache.load_letter_cache(resolved_instance, letter):
                series_tags = series.get("tags", [])
                for tag_id in series_tags:
                    if tag_id not in known_tags:
                        invalid_usages.append((series.get("id"), tag_id))

        if invalid_usages:
            self.logger.log_warning(f"🚫 Found {len(invalid_usages)} invalid tag references.")
            for sid, tid in invalid_usages[:10]:
                self.logger.log_info(f"   → Series {sid} uses unknown tag ID {tid}")
        else:
            self.logger.log_info("✅ All series tag references are valid.")
        return invalid_usages
