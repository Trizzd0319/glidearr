from datetime import datetime, timezone

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.config.cache_keys import CacheKeyPaths
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrQualityAdjustmentManager(BaseManager, ComponentManagerMixin):
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "SonarrQuality"
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
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))
        self.key_builder = kwargs.get("key_builder", getattr(parent, "key_builder", None))

        if not self.logger:
            raise ValueError(f"❌ {class_name} could not initialize without logger")

        self.logger.log_debug(f"🛠️ Initialized {class_name} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("list_adjustments")
    def list_adjustments(self):
        """List current quality adjustments from the API."""
        adjustments = self.sonarr_api._make_request("quality/adjustments") or []
        self.logger.log_info(f"📋 Retrieved {len(adjustments)} quality adjustments.")
        return adjustments

    @LoggerManager().log_function_entry
    @timeit("apply_adjustment")
    def apply_adjustment(self, adjustment_id, value):
        """Apply a specific adjustment by ID."""
        result = self.sonarr_api._make_request(
            f"quality/adjustments/{adjustment_id}",
            method="PUT",
            data={"value": value}
        )
        self.logger.log_info(f"✅ Applied adjustment {adjustment_id} with value {value}.")
        return result

    @LoggerManager().log_function_entry
    @timeit("remove_adjustment")
    def remove_adjustment(self, adjustment_id):
        """Remove a specific adjustment by ID."""
        result = self.sonarr_api._make_request(
            f"quality/adjustments/{adjustment_id}",
            method="DELETE"
        )
        self.logger.log_info(f"🗑️ Removed adjustment {adjustment_id}.")
        return result

    @LoggerManager().log_function_entry
    @timeit("refresh_adjustment_cache")
    def refresh_adjustment_cache(self):
        """Force-refresh the cache of quality adjustments."""
        adjustments = self.list_adjustments()
        self.global_cache.set("sonarr.quality.adjustments", adjustments)
        self.logger.log_info("🔄 Refreshed quality adjustment cache.")
        return adjustments

    def run_adjustment_data_pull(self):
        all_instances = self.sonarr_api.get_all_sonarr_apis()
        if not all_instances:
            raise RuntimeError("❌ No Sonarr instance found in config or API setup")

        for instance_name, arrapi_client in all_instances.items():
            self.logger.log_debug(f"📡 Pulling adjustment data from instance: {instance_name}")

            # Mount-deduped (root folders sharing a disk must not be summed twice).
            free  = self.sonarr_api.disk_free_bytes(instance_name)
            total = self.sonarr_api.disk_total_bytes(instance_name)
            if total in (0, float("inf")) or free == float("inf"):
                percent_free = 100.0   # unreadable → treat as no space pressure
            else:
                percent_free = round((free / total) * 100, 2)

            custom_boosts = {
                "x265": 15 if percent_free < 10 else 0,
                "4K": -10 if percent_free < 10 else 0,
                "HDR": 5 if percent_free > 20 else -5
            }

            cutoff_overrides = {
                "anime": "WebDL-1080p",
                "documentary": "HDTV-720p"
            }

            result = {
                "instance": instance_name,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "adjustments": {
                    "customFormatBoosts": custom_boosts,
                    "cutoffOverrides": cutoff_overrides,
                    "conditions": {
                        "spaceFreePercent": percent_free,
                        "preferredCodec": "x265",
                        "preferredContainer": "mkv"
                    }
                }
            }

            cache_key = self.key_builder.format_cache_key(CacheKeyPaths.sonarr.ADJUSTMENTS, instance=instance_name)
            self.global_cache.set_with_pretty_output(cache_key, result)
            self.logger.log_info(f"✅ Adjustments cached for {instance_name} (% free: {percent_free}%)")
