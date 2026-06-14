from .config_constants import ConfigGroups, SensitiveKeys


class ConfigSanitizer:
    def __init__(self, logger):
        self.logger = logger

    def log_redacted(self, config: dict, compact=True):
        self.logger.log_info("📋 Redacted Config Summary:")
        for group, prefix in ConfigGroups.SERVICE_KEYS.items():
            self.logger.log_info(f"🔹 {group}:")
            for k, v in config.items():
                if prefix in k:
                    redacted = self._redact(v, compact)
                    self.logger.log_info(f"   • {k}: {redacted}")

    def _redact(self, value, compact):
        if isinstance(value, dict):
            return {
                k: "[REDACTED]" if k.lower() in SensitiveKeys.DEFAULT else ("✅" if compact else str(v))
                for k, v in value.items()
            }
        return "[REDACTED]" if str(value).lower() in SensitiveKeys.DEFAULT else str(value)
