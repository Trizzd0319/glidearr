import re
from prettytable import PrettyTable

from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit

class RegistryCLI:
    def __init__(self, registry):
        self.registry = registry

    @staticmethod
    def _split_camel_case(name):
        """Split CamelCase string into components."""
        return re.findall(r'[A-Z][a-z]*|[a-z]+|\d+', name)

    def _is_expected_path(self, name, klass, origin_path):
        """Returns True if the origin path matches the expected service/module path derived from the class name."""
        if not origin_path or origin_path == "unknown":
            return False

        normalized = origin_path.replace("\\", "/").lower()

        try:
            base = klass.strip()
            for suffix in ["Manager"]:
                if base.endswith(suffix):
                    base = base[:-len(suffix)]

            for service in ["sonarr", "radarr", "trakt", "tautulli"]:
                if base.lower().startswith(service):
                    rel = base[len(service):]
                    components = [s.lower() for s in self._split_camel_case(rel)]
                    subpath = "/".join([service] + components)

                    if normalized.endswith(f"{subpath}.py"):
                        return True
                    if normalized.endswith("/__init__.py") and f"/{subpath}/" in normalized:
                        return True

        except Exception as e:
            LoggerManager().log_debug(f"[RegistryCLI] _is_expected_path error: {e}")

        return False

    @LoggerManager().log_function_entry
    @timeit("print_detailed_registry")
    def print_detailed_registry(self, category="manager"):
        logger = LoggerManager()
        entries = self.get_all(category)
        table = PrettyTable()
        table.title = f"📋 Registry Dump — Category: {category}"
        table.field_names = ["Manager", "Class", "Parent", "Source", "Anomaly"]
        table.align["Manager"] = "l"
        table.align["Class"] = "l"
        table.align["Parent"] = "l"
        table.align["Source"] = "l"
        table.align["Anomaly"] = "l"

        if not entries:
            table.add_row(["—"] * 5)
            logger.log_info(str(table))
            return

        for name, entry in entries.items():
            if isinstance(entry, dict):
                cls = entry.get("class", "Unknown")
                parent = entry.get("parent_name", "—")
                source = entry.get("source", "n/a")
            else:
                # fallback for direct manager objects
                cls = getattr(entry, "__class__", type(entry)).__name__
                parent = getattr(entry, "parent_name", "—")
                source = getattr(entry, "_registered_from", "n/a")

            anomaly = ""
            if isinstance(source, str) and "pycharmprojects" in source.lower():
                anomaly = "❌ Suspicious file path"

            table.add_row([name, cls, parent, source, anomaly])

        logger.log_info(str(table))
