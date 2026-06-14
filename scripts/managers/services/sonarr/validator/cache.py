from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrValidatorCacheManager(BaseManager, ComponentManagerMixin):
    """
    Validates whether expected cache keys exist per Sonarr instance.
    If missing, optionally triggers warm-up using the instance's API client.
    """

    REQUIRED_KEYS = [
        "sonarr.library",
        "sonarr.episodes",
        "sonarr.series",
        "sonarr.history",
        "sonarr.quality",
        "sonarr.tags"
    ]

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = self.__class__.__name__
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

        # 🔧 Dual cache setup
        manager = kwargs.get("manager") or {}
        self.sonarr_cache = kwargs.get("sonarr_cache") or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)

        self.register()

        self.manager = manager
        self.dry_run = getattr(self.manager, "dry_run", False)
        self.sonarr_apis = kwargs.get("sonarr_apis") or getattr(manager, "sonarr_apis", {})

        if not self.logger:
            raise ValueError(f"❌ {self.parent_name} could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {self.parent_name} (Dry Run = {self.dry_run})")

    @LoggerManager().log_function_entry
    @timeit("validate_sonarr_cache_keys")
    def run(self):
        for instance_name, api in self.sonarr_apis.items():
            self._validate_instance_cache(instance_name, api)

    def _validate_instance_cache(self, instance, api):
        self.logger.log_info(f"🔍 Validating cache for instance '{instance}'...")
        missing = []

        for key in self.REQUIRED_KEYS:
            full_key = f"{key}.{instance}"
            if not self.global_cache.exists(full_key):
                self.logger.log_warning(f"❌ Missing cache key → {full_key}")
                missing.append(key)

        if not missing:
            self.logger.log_success(f"✅ All {len(self.REQUIRED_KEYS)} cache keys valid for '{instance}'.")
        else:
            self.logger.log_info(f"🧩 {len(self.REQUIRED_KEYS)} checked, {len(missing)} missing → warming...")
            self._warm_missing_keys(instance, api, missing)

    def _warm_missing_keys(self, instance, api, keys):
        if self.dry_run:
            self.logger.log_info(f"💤 [Dry Run] Would warm: {keys} for {instance}")
            return

        for key in keys:
            try:
                result = api._make_request(instance, key) or {}
                self.global_cache.set(f"{key}.{instance}", result)
                self.logger.log_info(f"🔥 Warmed → {key}.{instance}")
            except Exception as e:
                self.logger.log_warning(f"⚠️ Failed to warm key {key}.{instance}: {e}")
