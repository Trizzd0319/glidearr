import asyncio
from collections import defaultdict

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrSeriesSyncAsyncManager(BaseManager, ComponentManagerMixin):
    """
    Handles asynchronous synchronization of Sonarr series across multiple instances.
    Applies tag rules like "keep" and pushes updates if differences are found.
    """

    def __init__(self, logger=None, config=None, global_cache=None, cache_manager=None,
                 validator=None, registry=None, **kwargs):

        self.parent_name = "SonarrSeries"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager") or self.registry.get("manager", self.parent_name)
        self.manager = parent
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(parent, "sonarr_api", None)
        self.logger = self.logger or getattr(parent, "logger", None)
        self.orchestration = kwargs.get("orchestration") or getattr(parent, "orchestration", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.dry_run = kwargs.get("dry_run", getattr(parent, "dry_run", False))

        # 🔧 Dual-cache
        self.sonarr_cache = cache_manager or getattr(parent, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(parent, "global_cache", None)

        # Keep-tag monitor (resolved/created via BaseManager.get_tag_monitor)
        self.tag_monitor = self.get_tag_monitor()

        self.sync_failures = defaultdict(list)

        if not self.logger:
            raise ValueError("❌ SonarrSeriesSyncAsyncManager could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__}")

    @LoggerManager().log_function_entry
    @timeit("async_synchronize_series_across_instances")
    async def async_synchronize_series_across_instances(self, dry_run=False, rate_limit_delay=0.2):
        self.logger.log_info("🚀 Starting async series synchronization across instances...")
        tasks = []

        all_instances = self.instance_manager.get_all_sonarr_apis()

        for instance_name, api in all_instances.items():
            tasks.append(self._sync_instance_series(instance_name, api, dry_run, rate_limit_delay))

        await asyncio.gather(*tasks)

        if self.sync_failures:
            self.logger.log_warning("⚠️ Sync failures occurred:")
            for instance, failures in self.sync_failures.items():
                for title in failures:
                    self.logger.log_warning(f"   ⛔ {instance} → {title}")
        else:
            self.logger.log_info("✅ All series synced successfully.")

        self.logger.log_info("🎉 Completed async series synchronization.")

    async def _sync_instance_series(self, instance_name, api, dry_run=False, rate_limit_delay=0.2):
        series_list = api.get_all_series()
        for series in series_list:
            series_id = series.get("id")
            title = series.get("title", f"ID-{series_id}")
            current_tags = set(series.get("tags", []))
            updated_tags = set(current_tags)

            if self.tag_monitor and self.tag_monitor.is_series_tagged_keep(series_id):
                updated_tags.add("keep")
                self.logger.log_info(f"🔒 Enforcing 'keep' tag on '{title}' ({instance_name})")

            if updated_tags != current_tags:
                self.logger.log_info(f"✏️ Diff for '{title}': tags changed ({current_tags} → {updated_tags})")

            if dry_run:
                self.logger.log_info(f"🛑 DRY-RUN: Would update '{title}' on {instance_name}")
            else:
                try:
                    payload = {
                        "id": series_id,
                        "tags": list(updated_tags),
                        "monitored": series.get("monitored", False)
                    }
                    api.update_single_series(series_id, payload, instance_name)
                    self.logger.log_info(f"✅ Synced '{title}' on {instance_name}")
                except Exception as e:
                    self.logger.log_warning(f"❌ Failed to sync '{title}' on {instance_name}: {e}")
                    self.sync_failures[instance_name].append(title)

            await asyncio.sleep(rate_limit_delay)

        self.logger.log_info(f"📦 Finished syncing {len(series_list)} series on {instance_name}")
