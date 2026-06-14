# beta/managers/factories/config/config_cli.py

from .validator import ConfigValidator
from scripts.support.utilities.logger.logger import LoggerManager


class ConfigCLI:
    MAX_RETRIES = 3

    def __init__(self, config_manager, logger=None):
        self.logger = logger or LoggerManager()
        self.config_manager = config_manager
        self.validator = ConfigValidator(config=config_manager.config, logger=self.logger)

    def run_interactive_validation(self):
        """
        Validates each service one-by-one.
        If it fails, prompt interactively, then retry validation.
        Maximum 3 attempts per service.
        """
        failed_services = []

        for service in self.validator.SCHEMA:
            self.logger.log_info(f"\n🔍 Checking configuration for: {service}")
            attempt = 1
            while attempt <= self.MAX_RETRIES:
                valid = self.validator.validate_service(service)
                if valid:
                    self.logger.log_info(f"✅ {service.capitalize()} config is valid.")
                    break

                self.logger.log_warning(f"⚠️ Validation failed for {service} (attempt {attempt}/{self.MAX_RETRIES})")
                self.validator.prompt_for_service(service)
                attempt += 1

            if not self.validator.validate_service(service):
                self.logger.log_error(f"❌ {service} validation failed after {self.MAX_RETRIES} attempts.")
                failed_services.append(service)

        if failed_services:
            self.logger.log_error(
                f"❌ Could not fix configuration for: {', '.join(failed_services)}.\n"
                f"Please update your config.json manually."
            )
            raise RuntimeError("🛑 Configuration incomplete.")
        else:
            self.logger.log_info("💾 All services validated successfully. Saving updated config...")
            self.config_manager.set_bulk(self.validator.config)
            self.config_manager.save()
            self.logger.log_info("✅ Config saved to disk.")
