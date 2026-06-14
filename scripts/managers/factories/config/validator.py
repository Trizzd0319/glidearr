from typing import Optional, Dict, List, Tuple

from scripts.support.utilities.logger.logger import LoggerManager


class ConfigValidator:
    SCHEMA = {
        "trakt": {
            "path": "trakt",
            "required": ["client_id", "client_secret"],
            "instance_mode": False,
        },
        "tvdb": {
            "path": "tvdb",
            "required": ["token"],
            "instance_mode": False,
        },
        "tautulli": {
            "path": "tautulli",
            "required": ["base_url", "api"],
            "instance_mode": False,
        },
        "sonarr": {
            "path": "sonarr_instances",
            "required": ["base_url", "api"],
            "instance_mode": True,
        },
        "radarr": {
            "path": "radarr_instances",
            "required": ["base_url", "api"],
            "instance_mode": True,
        }
    }

    def __init__(self, config: dict, logger: Optional[LoggerManager] = None):
        self.config = config
        self.logger = logger or LoggerManager()
        self.missing_keys: List[Tuple[str, str, str]] = []  # (service, config_path, key or instance.key)

    def validate_all_services(self) -> bool:
        """Validate all services in schema"""
        valid = True
        for service in self.SCHEMA:
            result = self.validate_service(service)
            valid = valid and result
        return valid

    def validate_service(self, service: str) -> bool:
        schema = self.SCHEMA.get(service)
        if not schema:
            self.logger.log_warning(f"⚠️ No schema defined for service: {service}")
            return True

        path = schema["path"]
        required_keys = schema["required"]
        instance_mode = schema.get("instance_mode", False)
        valid = True

        block = self.config.get(path, {})

        if instance_mode:
            for name, instance in block.items():
                if name == "default_instance":
                    continue
                for key in required_keys:
                    if not instance.get(key):
                        self.missing_keys.append((service, f"{path}.{name}", key))
                        self.logger.log_warning(f"❌ Missing key: {path}.{name}.{key}")
                        valid = False
        else:
            for key in required_keys:
                if not block.get(key):
                    self.missing_keys.append((service, path, key))
                    self.logger.log_warning(f"❌ Missing key: {path}.{key}")
                    valid = False

        return valid

    def prompt_for_service(self, service: str):
        """Prompt user for missing config keys from a single service"""
        if not self.missing_keys:
            self.logger.log_info(f"✅ No missing fields to prompt for '{service}'")
            return

        self.logger.log_info(f"🧩 Prompting for missing fields in: {service}")

        for svc, path, key in list(self.missing_keys):  # copy to modify safely
            if svc != service:
                continue

            section = self._get_nested_section(path)
            if not isinstance(section, dict):
                self._set_nested_section(path, {})
                section = self._get_nested_section(path)

            value = input(f"Enter value for {path}.{key}: ").strip()
            section[key] = value
            self.missing_keys.remove((svc, path, key))

        self.logger.log_info(f"✅ Fields for '{service}' updated in memory.")

    def prompt_if_invalid(self, service: str):
        if not self.validate_service(service):
            self.prompt_for_service(service)
            return self.validate_service(service)
        return True

    def _get_nested_section(self, path: str) -> dict:
        """Resolve a dotted path like 'sonarr_instances.4k' into a nested dict"""
        parts = path.split(".")
        section = self.config
        for part in parts:
            section = section.setdefault(part, {})
        return section

    def _set_nested_section(self, path: str, value: dict):
        parts = path.split(".")
        section = self.config
        for part in parts[:-1]:
            section = section.setdefault(part, {})
        section[parts[-1]] = value

    def get_missing_fields(self, service: Optional[str] = None) -> List[Tuple[str, str, str]]:
        return [m for m in self.missing_keys if m[0] == service or service is None]
