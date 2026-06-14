from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrSyncMediaManager(BaseManager, ComponentManagerMixin):
    """
    Synchronises media management settings and quality profiles across Radarr instances.
    """

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "RadarrSyncManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.dry_run          = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    def _resolve_instance(self, instance):
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    def _get_all_instances(self) -> list:
        if self.radarr_api and hasattr(self.radarr_api, "get_all_radarr_apis"):
            try:
                return list(self.radarr_api.get_all_radarr_apis().keys())
            except Exception:
                pass
        return list((self.config.get("radarr_instances") or {}).keys())

    @LoggerManager().log_function_entry
    @timeit("get_media_management_settings")
    def get_media_management_settings(self, instance: str) -> dict:
        resolved = self._resolve_instance(instance)
        return self.radarr_api._make_request(resolved, "config/mediamanagement", fallback={}) or {}

    @LoggerManager().log_function_entry
    @timeit("sync_media_management_settings")
    def sync_media_management_settings(self, settings: dict):
        """Sync provided media management settings to all Radarr instances."""
        instances = self._get_all_instances()
        if not instances:
            self.logger.log_warning("No Radarr instances found in config. Aborting media sync.")
            return

        for instance in instances:
            resolved = self._resolve_instance(instance)
            try:
                current = self.radarr_api._make_request(resolved, "config/mediamanagement", fallback={}) or {}
                if not isinstance(current, dict):
                    self.logger.log_warning(f"Invalid media management structure from {resolved}. Skipping.")
                    continue

                current.update(settings)
                if self.dry_run:
                    self.logger.log_info(f"[dry_run] Would sync media management settings to {resolved}")
                    continue

                result = self.radarr_api._make_request(
                    resolved, "config/mediamanagement", method="PUT", payload=current
                )
                status = "Synced" if result else "Failed to sync"
                self.logger.log_info(f"{status} media management settings for {resolved}")
            except Exception as e:
                self.logger.log_error(f"Media settings sync failed for {resolved}: {e}")

    @LoggerManager().log_function_entry
    @timeit("get_metadata")
    def get_metadata(self, instance: str) -> list:
        resolved = self._resolve_instance(instance)
        cached = self.global_cache.get(f"radarr.metadata.{resolved}", default=None)
        if cached is not None:
            return cached
        metadata = self.radarr_api._make_request(resolved, "metadata", fallback=[]) or []
        self.global_cache.set(f"radarr.metadata.{resolved}", metadata)
        return metadata

    @LoggerManager().log_function_entry
    @timeit("sync_quality_across_instances")
    def sync_quality_across_instances(self):
        """Synchronise quality profiles and custom formats across all Radarr instances."""
        all_instances = self._get_all_instances()
        if not all_instances:
            self.logger.log_error("No Radarr instances found for quality sync.")
            return

        reference = self._resolve_instance(all_instances[0])
        ref_profiles = self.radarr_api._make_request(reference, "qualityprofile", fallback=[]) or []
        ref_formats  = self.radarr_api._make_request(reference, "customformat", fallback=[]) or []

        for instance in all_instances[1:]:
            resolved = self._resolve_instance(instance)
            self.logger.log_info(f"Syncing quality from {reference} → {resolved}")

            if self.dry_run:
                self.logger.log_info(f"[dry_run] Would sync {len(ref_profiles)} profiles and {len(ref_formats)} formats to {resolved}")
                continue

            for profile in ref_profiles:
                self.radarr_api._make_request(resolved, "qualityprofile", method="POST", payload=profile)
            for fmt in ref_formats:
                self.radarr_api._make_request(resolved, "customformat", method="POST", payload=fmt)

        self.logger.log_info("Quality sync complete across all instances.")
