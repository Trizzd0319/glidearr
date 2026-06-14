import asyncio
from collections import defaultdict

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit


class SonarrSeriesSyncSynchronizeManager(BaseManager, ComponentManagerMixin):
    """
    Responsible for synchronizing series state (tags, monitored status, etc.)
    across Sonarr instances, including tag enforcement like 'keep'.
    """

    def __init__(self, logger=None, config=None, global_cache=None, cache_manager=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = "SonarrSeries"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager") or self.registry.get("manager", self.parent_name)

        self.manager = kwargs.get("manager", parent)
        self.logger = self.logger or getattr(parent, "logger", None)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(parent, "sonarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.tag_monitor = self.get_tag_monitor()

        # 🔧 Dual cache support
        self.global_cache = global_cache or getattr(parent, "global_cache", None)
        self.sonarr_cache = cache_manager or getattr(parent, "sonarr_cache", None)

        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))
        self.sync_failures = defaultdict(list)

        if not self.sonarr_api or not self.instance_manager:
            self.logger.log_warning("⚠️ SonarrSeriesSyncSynchronizeManager: API or instance_manager not resolved — sync operations will be unavailable.")

        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("async_synchronize_series_across_instances")
    async def async_synchronize_series_across_instances(self, dry_run=False, rate_limit_delay=0.2):
        self.logger.log_info("🚀 Starting async Sonarr series synchronization across instances...")
        tasks = []

        all_instances = self.instance_manager.get_all_sonarr_apis()

        for instance_name, api in all_instances.items():
            tasks.append(self._sync_instance_series(instance_name, api, dry_run, rate_limit_delay))

        await asyncio.gather(*tasks)

        if self.sync_failures:
            self.logger.log_warning("⚠️ Sync failures encountered:")
            for instance, errors in self.sync_failures.items():
                for title in errors:
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
                self.logger.log_info(f"🔒 Ensuring 'keep' tag on '{title}' ({instance_name})")

            original_monitored = series.get("monitored", False)
            new_payload = {
                "id": series_id,
                "tags": list(updated_tags),
                "monitored": original_monitored
            }

            tag_diff = updated_tags != current_tags
            if tag_diff:
                self.logger.log_info(f"✏️ Tag diff for '{title}': {current_tags} → {updated_tags}")

            if dry_run:
                self.logger.log_info(f"🛑 DRY-RUN: Would update '{title}' on {instance_name}")
            else:
                try:
                    api.update_single_series(series_id, new_payload, instance_name)
                    self.logger.log_info(f"✅ Synced '{title}' on {instance_name}")
                except Exception as e:
                    self.logger.log_warning(f"❌ Failed to sync '{title}' on {instance_name}: {e}")
                    self.sync_failures[instance_name].append(title)

            await asyncio.sleep(rate_limit_delay)

        self.logger.log_info(f"📦 Finished syncing {len(series_list)} series on {instance_name}")

    @LoggerManager().log_function_entry
    @timeit("run_sync_jobs")
    async def run_sync_jobs(self, sync_jobs, dry_run=False, rate_limit_delay=0.2):
        """
        Apply a pre-built list of sync jobs produced by composite_sync_workflow.

        Each job is ``{"instance": <name>, "title": <str>,
        "payload": {"id", "tags", "monitored"}}``.

        For each job (non-dry) we GET the full series, apply the tag/monitored
        changes, and PUT it back — the standard _make_request pattern used
        elsewhere — skipping the write when nothing actually changed. Respects
        dry_run (logs only).
        """
        if not sync_jobs:
            self.logger.log_info("📭 No sync jobs to run.")
            return

        applied = skipped = failed = 0
        _rows = []
        for job in sync_jobs:
            instance_name = job.get("instance")
            title         = job.get("title", "?")
            payload       = job.get("payload") or {}
            sid           = payload.get("id")
            if sid is None:
                continue

            want_tags = payload.get("tags")
            want_mon  = payload.get("monitored")

            if dry_run:
                _tags_list = list(want_tags) if want_tags is not None else []
                _tags_str = "[" + ",".join(str(t) for t in _tags_list) + "]"
                _rows.append([
                    str(title),
                    str(instance_name),
                    _tags_str,
                    "yes" if want_mon else "no",
                ])
                applied += 1
                await asyncio.sleep(0)
                continue

            try:
                series = self.sonarr_api._make_request(
                    instance_name, f"series/{sid}", fallback=None
                )
                if not (series and isinstance(series, dict)):
                    raise RuntimeError("series not found")

                new_tags = list(want_tags) if want_tags is not None else series.get("tags", [])
                new_mon  = bool(want_mon) if want_mon is not None else series.get("monitored", False)

                if (set(series.get("tags", [])) == set(new_tags)
                        and bool(series.get("monitored", False)) == new_mon):
                    skipped += 1
                    self.logger.log_debug(f"↔️ '{title}' already in sync — skipping")
                    await asyncio.sleep(0)
                    continue

                series["tags"]      = new_tags
                series["monitored"] = new_mon
                self.sonarr_api._make_request(
                    instance_name, f"series/{sid}", method="PUT", payload=series
                )
                applied += 1
                self.logger.log_info(f"✅ Synced '{title}' on {instance_name}")
            except Exception as e:
                failed += 1
                self.logger.log_warning(f"❌ Failed to sync '{title}' on {instance_name}: {e}")
                self.sync_failures[instance_name].append(title)

            await asyncio.sleep(rate_limit_delay)

        self.logger.log_grid(
            ["Title", "Instance", "Tags", "Monitored"],
            _rows,
            title="Sonarr series sync (dry-run)",
            cap=28,
        )

        self.logger.log_info(
            f"📦 Sync jobs complete: {applied} applied, {skipped} unchanged, "
            f"{failed} failed{' (dry-run)' if dry_run else ''}"
        )
