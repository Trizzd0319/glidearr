from datetime import datetime, timezone

import requests

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.config.cache_keys import CacheKeyPaths
from scripts.support.config.cache_keys import CacheKeyPaths as Paths
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrStorageSpaceManager(BaseManager, ComponentManagerMixin):
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "SonarrStorage"
        class_name = self.__class__.__name__

        if class_name.endswith("Manager"):
            self.parent_name = class_name.replace("Manager", "")
        else:
            self.parent_name = class_name

        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = self.registry.get("manager", self.parent_name)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(parent, "sonarr_api", None)
        self.logger = self.logger or getattr(parent, "logger", None)
        self.manager = kwargs.get("manager") or getattr(parent, "manager", None)
        self.cache_manager = kwargs.get("cache_manager") or getattr(self.manager, "cache_manager", None)
        self.key_builder = kwargs.get("key_builder") or getattr(self.manager, "key_builder", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))
        self.instance_manager = kwargs.get("instance_manager") or getattr(self.manager, "instance_manager", None)

        if not self.logger:
            raise ValueError(f"❌ {class_name} could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {class_name} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("get_free_space_per_instance")
    def get_free_space_per_instance(self):
        result = {}
        instances = self.config.get_sonarr_instances()
        if not instances:
            self.logger.log_warning("⚠️ No Sonarr instance found in config.")
            return result

        for instance in instances:
            resolved_instance = instance if isinstance(instance, str) else self.instance_manager.resolve_name(instance)
            root_folders = self.get_root_folders(resolved_instance)

            # Mount-deduped free space; clamp inf (no roots/unreadable) → 0.0 to
            # preserve selection/min() behavior for misconfigured instances.
            _free = self.sonarr_api.disk_free_gb(resolved_instance)
            result[resolved_instance] = round(_free if _free != float("inf") else 0.0, 2)
            self.logger.log_debug(f"📦 {resolved_instance} has {result[resolved_instance]} GB free.")
        return result

    @LoggerManager().log_function_entry
    @timeit("get_minimum_free_space")
    def get_minimum_free_space(self):
        space_by_instance = self.get_free_space_per_instance()
        min_space = min(space_by_instance.values()) if space_by_instance else 0
        self.logger.log_info(f"📉 Minimum free space across all instance: {min_space:.2f} GB")
        return min_space

    @LoggerManager().log_function_entry
    @timeit("get_root_folders")
    def get_root_folders(self, instance):
        resolved_instance = instance if isinstance(instance, str) else self.instance_manager.resolve_name(instance)
        self.logger.log_debug(f"🔍 Caching root folders for instance: {resolved_instance}")
        key = self.cache_manager.format_cache_key(Paths.sonarr.SPACE_ESTIMATES, instance=resolved_instance)
        return self.cache_manager.get_or_generate_cache(
            key=key,
            generator_function=lambda: self._fetch_root_folders(resolved_instance),
        )

    @LoggerManager().log_function_entry
    @timeit("_fetch_root_folders")
    def _fetch_root_folders(self, instance):
        return self.sonarr_api._make_request(instance, "rootfolder", fallback=[])

    @staticmethod
    @LoggerManager().log_function_entry
    @timeit("warm_cache")
    def warm_cache(logger, cache, config):

        instance = config.get_default_sonarr_instance_name()
        manager = SonarrStorageSpaceManager(logger=logger, config=config, global_cache=cache)
        cache.get_or_generate_cache(
            key=CacheKeyPaths.sonarr.SPACE_ESTIMATES,
            generator_function=lambda: manager.get_root_folders(instance),
            expiration_time=300,
        )

    @LoggerManager().log_function_entry
    @timeit("run_storage_data_pull")
    def run_storage_data_pull(self, instance):
        api_map = self.sonarr_api.get_all_sonarr_apis()
        all_instances = [(str(name), client) for name, client in api_map.items()]

        for instance_name, arrapi_client in all_instances:
            instance_config = (self.config.get("sonarr_instances") or {}).get(instance_name)
            if not instance_config:
                self.logger.log_error(f"❌ No configuration found for instance '{instance_name}'")
                continue

            api_base = instance_config['base_url']
            api_key = instance_config['api']

            url = f"{api_base}/api/v3/diskspace"
            params = {}

            try:
                response = requests.get(url, params=params, headers={"X-Api-Key": api_key})
                response.raise_for_status()
                disk_data = response.json()
            except Exception as e:
                self.logger.log_error(f"❌ Failed to fetch disk space info for '{instance_name}': {e}")
                continue

            # Prepare data
            serialized = [
                {
                    "path": d.get("path"),
                    "label": d.get("label"),
                    "freeSpace": d.get("freeSpace"),
                    "totalSpace": d.get("totalSpace"),
                    "unmappedFolders": d.get("unmappedFolders", [])
                }
                for d in disk_data
            ]

            # Save to cache
            cache_key = self.cache_manager.format_cache_key(
                Paths.sonarr.SPACE_ESTIMATES, instance=instance_name
            )
            updated_cache = {
                "diskspace": serialized,
                "meta": {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "instance": instance_name,
                    "count": len(serialized)
                }
            }
            self.cache_manager.set_with_pretty_output(cache_key, updated_cache)

            self.logger.log_info(
                f"✅ Disk space info cached for {instance_name} ({len(serialized)} entries)"
            )

    # Inside SonarrStorageSpaceManager
    def get_root_folder_map(self, instance):
        resolved_instance = self.resolve_instance(instance)
        return self.global_cache.get_or_generate_cache(
            key=f"sonarr/{resolved_instance}/storage",
            generator_function=lambda: self.sonarr_api.get_root_folders(resolved_instance)
        )

    @LoggerManager().log_function_entry
    @timeit("prompt_for_filesystem_total_if_missing")
    def prompt_for_filesystem_total_if_missing(self, root_path: str) -> int:
        """
        Prompts the user for total filesystem size if `shutil.disk_usage` fails to determine it.
        Stores the result in the global cache under `sonarr/manual_fs_total/<instance>`.
        """
        from pathlib import Path

        # Attempt to resolve instance from root_path
        matched_instance = None
        for instance in self.config.get_sonarr_instances():
            root_folders = self.get_root_folders(instance)
            for folder in root_folders:
                if Path(folder.get("path")) == Path(root_path):
                    matched_instance = instance
                    break

        if not matched_instance:
            self.logger.log_warning(f"❓ Unable to match '{root_path}' to a known Sonarr instance.")
            return 0

        try:
            user_input = input(f"❓ Could not determine total size for '{root_path}'. Enter total size in GB: ")
            total_size_gb = float(user_input.strip())
            total_bytes = int(total_size_gb * (1024 ** 3))
        except Exception as e:
            self.logger.log_error(f"❌ Invalid input for filesystem size: {e}")
            return 0

        cache_key = f"sonarr/manual_fs_total/{matched_instance}"
        existing = self.global_cache.get(cache_key, default=[])

        # Update or append
        updated = False
        for entry in existing:
            if entry.get("path") == root_path:
                entry["totalSpace"] = total_bytes
                updated = True
                break

        if not updated:
            existing.append({"path": root_path, "totalSpace": total_bytes})

        self.global_cache.set_with_pretty_output(cache_key, existing)
        self.logger.log_info(f"💾 Cached total space override for '{root_path}' = {total_size_gb:.2f} GB")
        return total_bytes
