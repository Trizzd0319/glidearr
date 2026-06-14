import inspect
import pathlib
from pathlib import Path
from typing import Any, Union


class SonarrInstanceResolverMixin:
    """
    Provides a unified resolve_instance helper for Sonarr submanagers.
    Requires self.instance_manager to be set (e.g., injected by SonarrManager).
    """

    def resolve_instance(self, instance: Union[str, Any]) -> str:
        if not hasattr(self, "instance_manager") or self.instance_manager is None:
            raise AttributeError(
                f"{self.__class__.__name__} requires 'instance_manager' to use resolve_instance()."
            )
        return self.instance_manager.resolve_instance(instance)


class ProjectPathMixin:
    """
    Provides project-root-relative path helpers for accessing cache, logs, and support files.
    """

    # 🔧 Adjust depth based on project structure
    PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

    def build_cache_path(self, *parts: Union[str, Path]) -> Path:
        return self.PROJECT_ROOT / "cache" / Path(*parts)

    def build_log_path(self, *parts: Union[str, Path]) -> Path:
        return self.PROJECT_ROOT / "support" / "logs" / Path(*parts)

    def build_support_path(self, *parts: Union[str, Path]) -> Path:
        return self.PROJECT_ROOT / "support" / Path(*parts)

class ManagerAttributionMixin:
    def _verify_parent_and_manager(self):
        """
        Ensures that 'manager' and 'parent_name' attributes are populated.
        Uses the module file path to infer service type (e.g., Sonarr, Radarr).
        """
        cls = self.__class__
        class_name = cls.__name__

        # Infer from module path: services/sonarr/cache.py → parent = SonarrCache
        try:
            file_path = inspect.getfile(cls)
            parts = pathlib.Path(file_path).parts
            if "services" in parts:
                service_idx = parts.index("services") + 1
                service = parts[service_idx].capitalize()
                module = parts[service_idx + 1].capitalize() if len(parts) > service_idx + 1 else ""
                inferred_parent = f"{service}{module}" if module else service
            else:
                inferred_parent = class_name.replace("Manager", "")
        except Exception:
            inferred_parent = class_name.replace("Manager", "")

        if not hasattr(self, "parent_name") or not getattr(self, "parent_name", None):
            self.parent_name = inferred_parent

        if not hasattr(self, "manager") or getattr(self, "manager", None) is None:
            if hasattr(self, "registry"):
                self.manager = self.registry.get("manager", self.parent_name)

        # Logging
        if hasattr(self, "logger") and self.logger:
            if not getattr(self, "manager", None):
                self.logger.log_warning(f"⚠️ {class_name} missing 'manager' link.")
            if not getattr(self, "parent_name", None):
                self.logger.log_warning(f"⚠️ {class_name} missing 'parent_name' attribute.")
