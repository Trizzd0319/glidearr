from pathlib import Path
from scripts.support.utilities.logger.logger import LoggerManager
from .config_loader import ConfigLoader
from .config_sanitizer import ConfigSanitizer
from .config_resolver import ConfigResolver

# Absolute path to scripts/support/config/config.json, resolved from THIS module's
# location (…/scripts/managers/factories/config/__Init__.py → parents[3] == …/scripts).
# A bare relative "support/config/config.json" only resolves when the cwd happens to
# be scripts/, so any run from the repo root fell back to an empty config and logged
# "Config file not found" once per manager that loads config without an inherited one.
_DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "support" / "config" / "config.json"


class ConfigManager:
    def __init__(self, logger=None, config_path=None, **kwargs):
        self.logger = logger or LoggerManager()
        self.path = Path(config_path) if config_path else _DEFAULT_CONFIG
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
