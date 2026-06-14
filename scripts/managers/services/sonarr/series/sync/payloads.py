from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.registry import RegistryHelper


class SonarrSeriesSyncPayloadManager(BaseManager, ComponentManagerMixin):
    def __init__(self, logger=None, config=None, global_cache=None, cache_manager=None, validator=None, registry=None, **kwargs):
        self.parent_name = "SonarrSeries"

        # 🔧 Dual-cache support
        manager = kwargs.get("manager") or registry.get("manager", self.parent_name)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)
        self.sonarr_cache = cache_manager or getattr(manager, "sonarr_cache", None)

        self.manager = manager
        self.logger = logger or getattr(manager, "logger", None)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(manager, "sonarr_api", None)
        self.orchestration = kwargs.get("orchestration") or getattr(manager, "orchestration", None)
        self.dry_run = kwargs.get("dry_run", getattr(manager, "dry_run", False))

        super().__init__(self.logger, config, self.global_cache, validator, registry, **kwargs)
        self.register()

        if not self.logger:
            raise ValueError("❌ SonarrSeriesSyncPayloadManager could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("prepare_series_payload")
    def prepare_series_payload(self, metadata: dict, instance: str) -> dict:
        """Generates a Sonarr-compatible payload from series metadata."""
        resolved_instance = self.manager.instance_manager.resolve_instance(instance)
        instance_config = (self.config.get("sonarr_instances") or {}).get(resolved_instance, {})

        root_folder_path = instance_config.get("rootFolderPath", "/tv")
        quality_profile_id = metadata.get("qualityProfileId", 1)
        language_profile_id = metadata.get("languageProfileId", 1)

        slug = metadata.get("slug") or metadata.get("title", "untitled").lower().replace(" ", "-")
        monitored = metadata.get("monitored", False)

        payload = {
            "tvdbId": metadata.get("tvdbId"),
            "title": metadata.get("title"),
            "titleSlug": slug,
            "qualityProfileId": quality_profile_id,
            "languageProfileId": language_profile_id,
            "seasonFolder": True,
            "monitored": monitored,
            "rootFolderPath": root_folder_path,
            "seriesType": metadata.get("seriesType", "standard"),
            "tags": metadata.get("tags", []),
        }

        self.logger.log_info(f"📦 Prepared Sonarr series payload for '{metadata.get('title')}' in {resolved_instance}")
        return payload

    @LoggerManager().log_function_entry
    @timeit("validate_series_payload")
    def validate_series_payload(self, payload: dict) -> bool:
        required_fields = ["tvdbId", "title", "titleSlug", "qualityProfileId", "rootFolderPath"]
        missing = [field for field in required_fields if not payload.get(field)]

        if missing:
            self.logger.log_warning(f"⚠️ Payload missing required fields: {missing}")
            return False

        self.logger.log_debug(f"✅ Payload validation passed for: {payload.get('title')}")
        return True
