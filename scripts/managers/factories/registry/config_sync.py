# registry/config_sync.py
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit

class RegistryConfigSync:
    def __init__(self, registry):
        self.registry = registry

    @LoggerManager().log_function_entry
    @timeit("auto_hot_swap_from_config")
    def auto_hot_swap_from_config(self, obj):
        if hasattr(obj, '_registry_category') and hasattr(obj, 'name'):
            self.registry.register(obj._registry_category, obj.name, obj)
        else:
            if hasattr(self.registry, "logger"):
                self.registry.logger.log_warning(
                    f"⚠️ Cannot auto-register object of type {type(obj)}: missing required attributes."
                )

    @LoggerManager().log_function_entry
    @timeit("load_config_and_propagate")
    def load_config_and_propagate(self, key):
        base_config = self.registry.get("manager", "ConfigManager")
        if not base_config:
            return
        value = getattr(base_config, key, None)
        if value is None:
            return
        for cat, entries in self.registry._registry.items():
            for name, obj in entries.items():
                if hasattr(obj, key):
                    setattr(obj, key, value)
