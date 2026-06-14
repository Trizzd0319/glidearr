# registry/injection.py
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit

class RegistryInjection:
    def __init__(self, registry):
        self.registry = registry

    @LoggerManager().log_function_entry
    @timeit("inject_dependencies_for_subtree")
    def inject_dependencies_for_subtree(self, root_name, category="manager"):
        root = self.registry.get(category, root_name)
        if not root:
            return
        for name, obj in self.registry.get_all(category).items():
            if getattr(obj, "parent_name", None) == root_name:
                for attr in ["logger", "config", "global_cache", "validator"]:
                    if hasattr(root, attr):
                        setattr(obj, attr, getattr(root, attr))
                setattr(obj, "registry", self.registry)
                if hasattr(obj, "_log_init_summary"):
                    obj._log_init_summary()
                self.inject_dependencies_for_subtree(name, category=category)
