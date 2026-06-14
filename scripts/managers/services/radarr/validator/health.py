from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrValidatorHealthManager(BaseManager, ComponentManagerMixin):
    """
    Verifies Radarr instance reachability and API health via system/status and health endpoints.
    """

    parent_name = "RadarrValidatorManager"

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
    @timeit("verify_api_health")
    def verify_api_health(self, instance: str = None) -> bool:
        instances = self._get_all_instances()
        target = instance or (instances[0] if instances else "default")
        return self._check_instance_health(target)

    @LoggerManager().log_function_entry
    @timeit("verify_all_instances_health")
    def verify_all_instances_health(self) -> dict:
        results = {}
        for instance in self._get_all_instances():
            results[instance] = self._check_instance_health(instance)

        healthy   = [k for k, v in results.items() if v]
        unhealthy = [k for k, v in results.items() if not v]
        self.logger.log_info(f"Health summary → Healthy: {healthy} | Unhealthy: {unhealthy}")
        return results

    @LoggerManager().log_function_entry
    @timeit("_check_instance_health")
    def _check_instance_health(self, instance: str) -> bool:
        try:
            response = self.radarr_api._make_request(instance, "system/status", fallback=None)
            if response and "version" in response:
                self.logger.log_info(f"{instance} healthy (v{response['version']})")
                return True
            self.logger.log_warning(f"{instance} returned unexpected health response.")
            return False
        except Exception as e:
            self.logger.log_error(f"Health check failed for '{instance}': {e}")
            return False

    @LoggerManager().log_function_entry
    @timeit("run_selftest")
    def run_selftest(self) -> dict:
        self.logger.log_info("Running Radarr self-test...")
        results = self.verify_all_instances_health()
        self.logger.log_info(f"Self-test results: {results}")
        return results
