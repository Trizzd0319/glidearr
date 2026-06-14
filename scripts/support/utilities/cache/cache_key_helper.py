class CacheKeyHelper:
    @staticmethod
    def build(key_template: str, **placeholders) -> str:
        """
        Replaces placeholders like <instance> or <user> in the given cache key template.

        Example:
            CacheKeyHelper.build(CacheKeyPaths.sonarr.EPISODES_ALL, instance="720")
        """
        resolved = key_template
        for placeholder, value in placeholders.items():
            resolved = resolved.replace(f"<{placeholder}>", value)
        return resolved

    @staticmethod
    def list_service_keys(service_prefix: str, cache_keys_class) -> list:
        """
        Lists all keys under a service class (like CacheKeyPaths.sonarr).

        Example:
            CacheKeyHelper.list_service_keys("sonarr", CacheKeyPaths.sonarr)
        """
        keys = []
        for attr in dir(cache_keys_class):
            if attr.isupper():
                value = getattr(cache_keys_class, attr)
                if isinstance(value, str) and value.startswith(f"{service_prefix}/"):
                    keys.append(value)
        return keys
