from pathlib import Path
from scripts.support.utilities.logger.logger import LoggerManager
from .config_loader import ConfigLoader
from .config_sanitizer import ConfigSanitizer
from .config_resolver import ConfigResolver


class ConfigManager:
    def __init__(self, logger=None, config_path="support/config/config.json", **kwargs):
        self.logger = logger or LoggerManager()
        self.path = Path(config_path)
        self.loader = ConfigLoader(self.path, logger=self.logger)
        self.config = self.loader.load()

        # Detect an existing SecretStore (keyring survives reinstalls / a blank
        # config.json) or set it up from scratch on a first-run interactive TTY.
        # Best-effort: never blocks a headless run, never fatal.
        try:
            from .secret_bootstrap import SecretBootstrap
            SecretBootstrap(self.loader, self.logger).ensure(self.config)
        except Exception as e:
            self.logger.log_warning(f"[SecretBootstrap] skipped: {e}")

        self.sanitizer = ConfigSanitizer(self.logger)
        self.resolver = ConfigResolver(self.config, self.logger)

    def get(self, key, default=None):
        return self.config.get(key, default)

    def set(self, key, value):
        self.config[key] = value
        self.loader.save(self.config)

    def reload(self):
        self.config = self.loader.load()

    def get_sonarr_instances(self):
        return self.resolver.get_instances("sonarr")

    def get_default_sonarr_instance(self):
        return self.resolver.get_default_instance("sonarr")

    def log_safe_config(self):
        self.sanitizer.log_redacted(self.config)

    def set_bulk(self, new_config: dict):
        self.config.update(new_config)

    def save(self):
        self.loader.save(self.config)

    @property
    def raw_data(self):
        return self.config
