import requests

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit


class SonarrValidatorFactoryManager(BaseManager, ComponentManagerMixin):
    """
    Lightweight helper for raw Sonarr API GETs outside of formal models.
    Used by validator submodules for checking arbitrary endpoints (e.g. /system/status).
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = self.__class__.__name__
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

        # 🔧 Dual cache setup
        manager = kwargs.get("manager") or {}
        self.manager = manager or self.registry.get("manager", self.parent_name)
        self.sonarr_cache = kwargs.get("sonarr_cache") or getattr(self.manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(self.manager, "global_cache", None)

        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))
        self.default_headers = {
            "Content-Type": "application/json"
        }

        self.register()
        self.logger.log_debug(f"🧰 Initialized {self.parent_name} (Dry Run = {self.dry_run})")

    @LoggerManager().log_function_entry
    @timeit("get_raw")
    def get_raw(self, url, headers=None, timeout=5):
        """
        Makes a direct GET request with optional headers and timeout.
        """
        try:
            final_headers = {**self.default_headers, **(headers or {})}
            response = requests.get(url, headers=final_headers, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.logger.log_warning(f"⚠️ Failed raw GET to {url}: {e}")
            return {}

    @LoggerManager().log_function_entry
    @timeit("get_instance_raw")
    def get_instance_raw(self, instance_obj, endpoint):
        """
        Pulls endpoint data from a raw Sonarr instance dictionary using /api/v3 pathing.
        """
        try:
            base = instance_obj.get("base_url") or instance_obj.get("url")
            token = instance_obj.get("api") or instance_obj.get("api_key") or instance_obj.get("token")
            headers = {"X-Api-Key": token}

            full_url = f"{base.rstrip('/')}/api/v3/{endpoint.lstrip('/')}"
            return self.get_raw(full_url, headers=headers)
        except Exception as e:
            self.logger.log_warning(f"⚠️ Failed API call to {endpoint}: {e}")
            return {}
