# beta/managers/registry/__init__.py
from .core import RegistryCore
from .trace import RegistryTracer
from .cli import RegistryCLI
from .injection import RegistryInjection
from .health import RegistryHealth
from .config_sync import RegistryConfigSync

class RegistryManager(
    RegistryCore,
    RegistryTracer,
    RegistryCLI,
    RegistryInjection,
    RegistryHealth,
    RegistryConfigSync,
):
    def __init__(self):
        # Each mixin uses self.registry to reach core _registry if needed
        self._registry = getattr(self, "_registry", {})
        self.registry = self  # Alias for subcomponents expecting registry ref

    def set_flag(self, *args, **kwargs): return RegistryCore.set_flag(self, *args, **kwargs)
    def get_flag(self, *args, **kwargs): return RegistryCore.get_flag(self, *args, **kwargs)
    def has_flag(self, *args, **kwargs): return RegistryCore.has_flag(self, *args, **kwargs)
    def clear_flags(self, *args, **kwargs): return RegistryCore.clear_flags(self, *args, **kwargs)



# Singleton export
def get_registry():
    return RegistryManager()
