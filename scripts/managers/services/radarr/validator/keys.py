import getpass
import json
import os

import requests

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.cache import make_json_safe
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrValidatorKeysManager(BaseManager, ComponentManagerMixin):
    """
    Validates Radarr API keys, checks instance reachability, and backs up
    all instance configs to disk.
    """

    parent_name = "RadarrValidatorManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "RadarrValidatorManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.dry_run          = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    def _get_all_apis(self) -> dict:
        """Return {instance_name: api} map from instance_manager or radarr_api."""
        if self.radarr_api and hasattr(self.radarr_api, "get_all_radarr_apis"):
            try:
                return self.radarr_api.get_all_radarr_apis()
            except Exception:
                pass
        if self.instance_manager and hasattr(self.instance_manager, "get_all_radarr_apis"):
            try:
                return self.instance_manager.get_all_radarr_apis()
            except Exception:
                pass
        return {}

    @LoggerManager().log_function_entry
    @timeit("check_instance_reachability")
    def check_instance_reachability(self, url: str, api_key: str) -> bool:
        """Perform a direct HTTP health check against a Radarr URL."""
        try:
            headers = {"X-Api-Key": api_key}
            response = requests.get(f"{url}/api/v3/system/status", headers=headers, timeout=5)
            if response.status_code == 200 and "version" in response.json():
                return True
            self.logger.log_warning(f"API health check failed: {url} → Status {response.status_code}")
            return False
        except Exception as e:
            self.logger.log_warning(f"Connection error to {url}: {e}")
            return False

    @LoggerManager().log_function_entry
    @timeit("validate_all_keys")
    def validate_all_keys(self) -> dict:
        """
        Check reachability for every configured Radarr instance using the config API keys.
        Returns {instance_name: True/False}.
        """
        radarr_instances = self.config.get("radarr_instances", {})
        results = {}
        for name, cfg in radarr_instances.items():
            url = cfg.get("url") or cfg.get("base_url", "")
            key = cfg.get("api") or cfg.get("api_key", "")
            if not url or not key:
                self.logger.log_warning(f"Missing URL or API key for instance '{name}'")
                results[name] = False
                continue
            results[name] = self.check_instance_reachability(url, key)
        return results

    @LoggerManager().log_function_entry
    @timeit("backup_all_configs")
    def backup_all_configs(self, backup_path: str):
        """Export every instance's config/naming/qualityprofile to JSON files at backup_path."""
        os.makedirs(backup_path, exist_ok=True)
        all_apis = self._get_all_apis()

        endpoints = ["config/host", "config/naming", "config/mediamanagement", "qualityprofile"]
        for instance, api in all_apis.items():
            instance_data = {}
            for endpoint in endpoints:
                try:
                    data = api._make_request(instance, endpoint, fallback=None)
                    if data:
                        instance_data[endpoint.replace("/", "_")] = data
                except Exception as e:
                    self.logger.log_warning(f"Could not fetch {endpoint} from {instance}: {e}")

            if instance_data:
                path = os.path.join(backup_path, f"config_{instance}.json")
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(make_json_safe(instance_data), f, indent=2)
                    self.logger.log_info(f"Backed up config for {instance} → {path}")
                except Exception as e:
                    self.logger.log_error(f"Error writing backup for {instance}: {e}")
            else:
                self.logger.log_warning(f"No config data fetched for {instance}")

    @LoggerManager().log_function_entry
    @timeit("prompt_and_repair_instances")
    def prompt_and_repair_instances(self):
        """
        Interactive CLI wizard to reconfigure Radarr instances.
        Only intended for direct script invocation, not automated flows.
        """
        radarr_instances = {}
        try:
            target_count = int(input("How many Radarr instances should be configured? "))
        except ValueError:
            self.logger.log_error("Invalid number entered. Aborting repair.")
            return

        base_url = input("Enter base Radarr URL (e.g., http://localhost): ").strip()
        try:
            base_port = int(input("Enter starting port number (e.g., 7878): ").strip())
        except ValueError:
            self.logger.log_error("Invalid port number entered. Aborting repair.")
            return

        for idx in range(target_count):
            name = input(f"Name for instance {idx + 1} (e.g., '720', '1080', '4k'): ").strip()
            port = base_port + idx
            override = input(f"Default port for '{name}' is {port}. Override? (Enter to keep): ").strip()
            if override:
                try:
                    port = int(override)
                except ValueError:
                    self.logger.log_warning("Invalid port input, keeping default.")
            api_key = getpass.getpass(f"Radarr API key for '{name}': ").strip()
            full_url = f"{base_url}:{port}"
            radarr_instances[name] = {"url": full_url, "base_url": full_url, "api": api_key}

        failures = [
            name for name, cfg in radarr_instances.items()
            if not self.check_instance_reachability(cfg["url"], cfg["api"])
        ]

        if failures:
            print("\nThe following instances failed reachability checks:")
            for name in failures:
                print(f"  - {name}")
            if input("Re-enter settings for these? (yes/no): ").strip().lower() == "yes":
                for name in failures:
                    radarr_instances[name]["url"]     = input(f"URL for '{name}': ").strip()
                    radarr_instances[name]["base_url"] = radarr_instances[name]["url"]
                    radarr_instances[name]["api"]     = getpass.getpass(f"API key for '{name}': ").strip()
            else:
                self.logger.log_error("Aborting due to failed instance checks.")
                return

        if input("Confirm and save? (yes/no): ").strip().lower() != "yes":
            self.logger.log_info("Aborted by user. No changes saved.")
            return

        self.config["radarr_instances"] = radarr_instances
        self.logger.log_info("Repair complete. Persist the updated config to disk.")
        return self.config
