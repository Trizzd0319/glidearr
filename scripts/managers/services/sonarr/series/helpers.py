from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit


class SonarrSeriesHelpersManager(BaseManager, ComponentManagerMixin):
    """
    Utility and helper tools for Sonarr series-related operations:
    TVDB lookups, title slugification, and series matching.
    """

    parent_name = "SonarrSeries"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

        # 🔧 Dual-cache setup
        manager = kwargs.get("manager") or {}
        self.manager = manager
        self.sonarr_cache = kwargs.get("sonarr_cache") or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)
        self.dry_run = kwargs.get("dry_run", getattr(manager, "dry_run", False))

        self.sonarr_api = kwargs.get("sonarr_api") or getattr(manager, "sonarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(manager, "instance_manager", None)

        self.register()
        self.logger.log_debug(f"✅ Initialized {self.__class__.__name__} (Dual-cache ready)")

    def run(self):
        self.logger.log_info("🚀 SonarrSeriesHelpersManager run() called — no operational logic defined.")

    # -----------------------------
    # 🔧 Utility Methods
    # -----------------------------

    @LoggerManager().log_function_entry
    @timeit("sanitize_series_title")
    def sanitize_series_title(self, title: str) -> str:
        return title.replace("’", "'").strip().lower()

    @LoggerManager().log_function_entry
    @timeit("slugify_title")
    def slugify_title(self, title: str) -> str:
        return title.lower().replace(" ", "-").replace("'", "")

    @LoggerManager().log_function_entry
    @timeit("extract_tvdb_id_from_series")
    def extract_tvdb_id_from_series(self, series_obj: dict):
        return (
            series_obj.get("tvdbId")
            or series_obj.get("tvdb_id")
            or (series_obj.get("externalIds") or {}).get("tvdb")
        )

    @LoggerManager().log_function_entry
    @timeit("is_valid_tvdb_id")
    def is_valid_tvdb_id(self, tvdb_id) -> bool:
        return isinstance(tvdb_id, int) and tvdb_id > 0

    @LoggerManager().log_function_entry
    @timeit("get_series_by_tvdb")
    def get_series_by_tvdb(self, instance: str, tvdb_id: int):
        if not self.is_valid_tvdb_id(tvdb_id):
            self.logger.log_warning("⚠️ TVDB ID is missing or invalid.")
            return None

        resolved_instance = self.instance_manager.resolve_instance(instance)
        results = self.sonarr_api._make_request(resolved_instance, f"movies?tvdbId={tvdb_id}") or []

        if not results:
            self.logger.log_warning(f"⚠️ No series found in {resolved_instance} with TVDB ID {tvdb_id}")
            return None
        return results[0]

    @LoggerManager().log_function_entry
    @timeit("get_series_title_slug")
    def get_series_title_slug(self, instance: str, series_id: int):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        retrieval = self.registry.get("manager", "retrieval") or getattr(self.manager, "retrieval", None)

        if not retrieval:
            self.logger.log_warning("⚠️ Retrieval manager not found in registry.")
            return None

        series_data = retrieval.get_series_by_id(resolved_instance, series_id)
        if not series_data:
            self.logger.log_warning(f"⚠️ No data found for ID {series_id} in {resolved_instance}")
            return None

        return series_data.get("titleSlug")

    @LoggerManager().log_function_entry
    @timeit("get_series_title")
    def get_series_title(self, instance: str, series_id: int):
        data = self.get_series_by_id(instance, series_id)
        return data.get("title") if data else None

    @LoggerManager().log_function_entry
    @timeit("get_series_by_id")
    def get_series_by_id(self, instance: str, series_id: int):
        retrieval = self.registry.get("manager", "retrieval") or getattr(self.manager, "retrieval", None)

        if not retrieval:
            self.logger.log_warning("⚠️ Retrieval manager not found in registry.")
            return None

        return retrieval.get_series_by_id(instance, series_id)

    @LoggerManager().log_function_entry
    @timeit("get_series_tags")
    def get_series_tags(self, instance: str):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        return self.sonarr_api._make_request(resolved_instance, "tags", fallback=[])

    @LoggerManager().log_function_entry
    @timeit("generate_series_lookup_map")
    def generate_series_lookup_map(self, instance: str):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        return self.sonarr_api._make_request(resolved_instance, "movies", fallback=[])
