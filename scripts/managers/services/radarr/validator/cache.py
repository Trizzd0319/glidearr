from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrValidatorCacheManager(BaseManager, ComponentManagerMixin):
    """
    Validates that required Radarr cache keys are present and warms any that are missing.
    """

    parent_name = "RadarrValidatorManager"

    REQUIRED_KEYS = [
        "radarr.instance.metadata",
        "radarr.instance.health",
        "radarr.history",
        "radarr.tags",
        "radarr.quality_profiles",
        "radarr.custom_formats",
        "radarr.quality_definitions",
        "radarr.space_estimates",
    ]

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "RadarrValidatorManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.dry_run          = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    def _get_all_instances(self) -> list:
        if self.radarr_api and hasattr(self.radarr_api, "get_all_radarr_apis"):
            try:
                return list(self.radarr_api.get_all_radarr_apis().keys())
            except Exception:
                pass
        if self.instance_manager and hasattr(self.instance_manager, "get_all_radarr_apis"):
            try:
                return list(self.instance_manager.get_all_radarr_apis().keys())
            except Exception:
                pass
        return []

    @LoggerManager().log_function_entry
    @timeit("validate_all_instances")
    def validate_all_instances(self):
        """Validate cache keys for every configured Radarr instance."""
        for instance in self._get_all_instances():
            self.validate(instance)

    @LoggerManager().log_function_entry
    @timeit("validate")
    def validate(self, instance: str):
        self.logger.log_info(f"Validating Radarr cache keys for '{instance}'...")
        missing = []

        for key in self.REQUIRED_KEYS:
            full_key = f"{key}.{instance}"
            exists = (
                self.global_cache.exists(full_key)
                if hasattr(self.global_cache, "exists")
                else self.global_cache.get(full_key, default=None) is not None
            )
            if not exists:
                self.logger.log_warning(f"Missing cache key → {full_key}")
                missing.append(key)

        if not missing:
            self.logger.log_info(f"All required Radarr cache keys present for '{instance}'.")
        else:
            self.logger.log_info(
                f"{len(self.REQUIRED_KEYS)} keys checked, {len(missing)} missing → warming cache."
            )
            self._warm_cache_for_validation(instance, missing)

    @LoggerManager().log_function_entry
    @timeit("_warm_cache_for_validation")
    def _warm_cache_for_validation(self, instance: str, missing_keys: list):
        self.logger.log_info(f"Warming {len(missing_keys)} missing cache keys for {instance}...")
        for key in missing_keys:
            full_key = f"{key}.{instance}"
            try:
                # Derive API endpoint from key tail
                endpoint = key.split(".")[-1].replace("_", "/")
                data = self.radarr_api._make_request(instance, endpoint, fallback={}) or {}
                self.global_cache.set(full_key, data)
                self.logger.log_info(f"Warmed cache key: {full_key}")
            except Exception as e:
                self.logger.log_warning(f"Failed to warm cache for key {full_key}: {e}")
