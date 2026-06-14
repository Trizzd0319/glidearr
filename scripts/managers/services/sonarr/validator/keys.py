import getpass
import json
import os
import requests

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.cache import make_json_safe
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit


class SonarrValidatorKeysManager(BaseManager, ComponentManagerMixin):
    """
    Manages interactive setup and validation of Sonarr instance credentials.
    Includes live reachability checks, prompting, and config export/repair.
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = self.__class__.__name__
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

        # 🔧 Dual cache setup
        manager = kwargs.get("manager") or {}
        self.manager = manager
        self.sonarr_cache = kwargs.get("sonarr_cache") or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)
        self.dry_run = kwargs.get("dry_run", getattr(manager, "dry_run", False))

        self.register()

        if not self.logger:
            raise ValueError(f"❌ {self.parent_name} could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {self.parent_name} (Dry Run = {self.dry_run})")

    @LoggerManager().log_function_entry
    @timeit("validate_reachability")
    def check_instance_reachability(self, url: str, api_key: str) -> bool:
        try:
            headers = {"X-Api-Key": api_key}
            response = requests.get(f"{url}/api/v3/system/status", headers=headers, timeout=5)
            if response.status_code == 200 and "version" in response.json():
                return True
            self.logger.log_warning(f"⚠️ API health check failed for {url} → Status {response.status_code}")
            return False
        except Exception as e:
            self.logger.log_warning(f"⚠️ Connection error to {url}: {e}")
            return False

    @LoggerManager().log_function_entry
    @timeit("prompt_config_repair")
    def prompt_and_repair_instances(self):
        sonarr_instances = {}

        try:
            target_count = int(input("🔧 How many Sonarr instances should be configured? "))
        except ValueError:
            self.logger.log_error("❌ Invalid number entered. Aborting repair.")
            return

        base_url = input("🌍 Enter base Sonarr URL (e.g., http://localhost): ").strip()

        try:
            base_port = int(input("🔌 Enter starting port number (e.g., 8989): ").strip())
        except ValueError:
            self.logger.log_error("❌ Invalid port number entered. Aborting repair.")
            return

        for idx in range(target_count):
            name = input(f"➕ Name for instance {idx + 1} (e.g., 720, 1080, 4k): ").strip()
            port = base_port + idx
            override = input(f"Default port for '{name}' is {port}. Override? [Enter to keep]: ").strip()
            if override:
                try:
                    port = int(override)
                except ValueError:
                    self.logger.log_warning(f"⚠️ Invalid port override. Using default {port}.")

            api_key = getpass.getpass(f"🔑 Enter Sonarr API key for '{name}': ").strip()
            full_url = f"{base_url}:{port}"

            sonarr_instances[name] = {
                "base_url": full_url,
                "url": full_url,
                "api": api_key,
                "port": port
            }

        # Health check phase
        failures = []
        for name, cfg in sonarr_instances.items():
            self.logger.log_info(f"🌐 Checking {name} ({cfg['url']})...")
            if not self.check_instance_reachability(cfg['url'], cfg['api']):
                failures.append(name)

        if failures:
            print("\n❌ The following instances failed checks:")
            for name in failures:
                print(f" - {name}")
            if input("\n🔁 Re-enter for these? (yes/no): ").strip().lower() == "yes":
                for name in failures:
                    print(f"\n🔄 Reconfiguring '{name}'")
                    cfg = sonarr_instances[name]
                    cfg["url"] = cfg["base_url"] = input("🌍 Enter new URL: ").strip()
                    cfg["api"] = getpass.getpass("🔑 Enter new API key: ").strip()
            else:
                self.logger.log_error("🚫 Aborting due to unreachable instances.")
                return

        # Final confirmation
        print("\n📝 Final Config:")
        for name, cfg in sonarr_instances.items():
            print(f"\n[{name}]")
            for k, v in cfg.items():
                print(f"  {k}: {v}")

        confirm = input("\n✅ Confirm and save? (yes/no): ").strip().lower()
        if confirm == "yes":
            self.config["sonarr_instances"] = sonarr_instances
            self.logger.log_success("✅ Sonarr configuration repaired. Remember to persist the config.")
            return self.config
        else:
            self.logger.log_info("❌ Cancelled. No changes saved.")

    @LoggerManager().log_function_entry
    @timeit("backup_config")
    def backup_all_configs(self, backup_path):
        os.makedirs(backup_path, exist_ok=True)
        apis = self.registry.get_all("sonarr_api")

        for instance, api in apis.items():
            try:
                data = api._make_request(instance, "config")
                if data:
                    with open(f"{backup_path}/config_{instance}.json", "w", encoding="utf-8") as f:
                        json.dump(make_json_safe(data), f, indent=2)
                    self.logger.log_success(f"✅ Backed up config for {instance}")
                else:
                    self.logger.log_warning(f"⚠️ No config data returned from {instance}")
            except Exception as e:
                self.logger.log_error(f"❌ Backup failed for {instance}: {e}")

    def run_credentials_only(self):
        instances = self.config.get("sonarr_instances", {})
        valid, missing, errored = 0, 0, []

        for name, cfg in instances.items():
            try:
                api_key = cfg.get("api")
                if not api_key:
                    self.logger.log_warning(f"❌ Missing API key for {name}")
                    self.registry.set_flag(f"sonarr.instance.{name}.api_missing", True)
                    missing += 1
                else:
                    self.logger.log_debug(f"✅ API key present for {name}")
                    self.registry.set_flag(f"sonarr.instance.{name}.api_present", True)
                    valid += 1
            except Exception as e:
                errored.append(name)
                self.logger.log_error(f"❌ Error validating API key for '{name}': {e}")

        return {
            "valid": valid,
            "missing": missing,
            "errored": errored,
            "success": missing == 0 and not errored
        }
