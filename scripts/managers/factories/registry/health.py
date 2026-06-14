# registry/health.py
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit

class RegistryHealth:
    def __init__(self, registry):
        self.registry = registry

    @LoggerManager().log_function_entry
    @timeit("set_component_status")
    def set_component_status(self, key, status: bool):
        if "component_status" not in self.registry._registry:
            self.registry._registry["component_status"] = {}
        self.registry._registry["component_status"][key] = status

    @LoggerManager().log_function_entry
    @timeit("get_component_status")
    def get_component_status(self, key):
        return (self.registry._registry.get("component_status") or {}).get(key)

    @LoggerManager().log_function_entry
    @timeit("get_all_failed_components")
    def get_all_failed_components(self):
        return [
            key for key, status in (self.registry._registry.get("component_status") or {}).items()
            if status is False
        ]
