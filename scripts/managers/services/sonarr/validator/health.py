from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit


class SonarrValidatorHealthManager(BaseManager, ComponentManagerMixin):
    """
    Checks if all Sonarr instances are responding correctly to basic health pings
    by verifying the 'system/status' endpoint and logging success or failure.
    """

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = self.__class__.__name__

        # 🔧 Dual cache setup
        manager = kwargs.get("manager") or {}
        self.sonarr_cache = kwargs.get("sonarr_cache") or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)

        super().__init__(logger, config, self.global_cache, validator, registry, **kwargs)
        self.register()

        self.manager = manager or self.registry.get("manager", self.parent_name)
        self.sonarr_apis = kwargs.get("sonarr_apis") or getattr(self.manager, "sonarr_apis", {})

        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))
        if not self.logger:
            raise ValueError(f"❌ {self.parent_name} could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {self.parent_name} (Dry Run = {self.dry_run})")

    @LoggerManager().log_function_entry
    @timeit("run_health_check")
    def run(self):
        results = self._verify_all_instances_health()
        self.logger.log_info(f"📝 Health Check Summary: {results}")
        return results

    def _verify_all_instances_health(self) -> dict:
        results = {}
        for name, api in self.sonarr_apis.items():
            results[name] = self._check_instance_health(name, api)

        healthy = [k for k, v in results.items() if v]
        unhealthy = [k for k, v in results.items() if not v]
        self.logger.log_info(f"🩺 Sonarr API status → ✅ Healthy: {healthy} | ❌ Unhealthy: {unhealthy}")
        return results

    def _check_instance_health(self, name, api) -> bool:
        try:
            result = api._make_request(name, "system/status")
            if result and "version" in result:
                self.logger.log_success(f"✅ {name} responsive (v{result.get('version', 'n/a')})")
                return True
            self.logger.log_warning(f"⚠️ {name} returned unexpected system/status response.")
            return False
        except Exception as e:
            self.logger.log_error(f"❌ Health check failed for '{name}': {e}")
            return False
