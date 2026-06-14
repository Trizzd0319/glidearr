import asyncio
import concurrent.futures

from scripts.managers.factories.base_manager import BaseManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin


class RadarrOrchestrationCacheManager(BaseManager, ComponentManagerMixin):
    """
    Handles orchestration-level caching operations for Radarr:
    - Parallel warmups
    - Async refreshes
    - Summary caching
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "RadarrCacheManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.manager          = parent
        self.dry_run          = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    # ──────────────────────────────────────────────────────────────────────────────
    # 🔁 Parallel Warmup (Threaded)
    # ──────────────────────────────────────────────────────────────────────────────
    @timeit("warm_instance_caches")
    def warm_instance_caches(self, instances, cache_tasks):
        """
        Runs cache warmups in parallel for multiple instance using thread pool.
        Each task must be a callable accepting (instance) as arg.
        """
        if not isinstance(cache_tasks, dict):
            self.logger.log_error(f"❌ Invalid cache_tasks type: expected dict, got {type(cache_tasks).__name__}")
            return

        # Bound the pool so it can't spawn an unbounded number of workers.
        max_workers = min(8, max(1, len(cache_tasks)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(task, instance)
                for instance, task in cache_tasks.items()
            ]
            for future in concurrent.futures.as_completed(futures):
                try:
                    result = future.result()
                    self.logger.log_info(f"✅ Cache warm completed for: {result}")
                except Exception as e:
                    self.logger.log_error(f"❌ Cache warm failed: {e}")

    # ──────────────────────────────────────────────────────────────────────────────
    # ⚡ Async Bulk Cache Refresh
    # ──────────────────────────────────────────────────────────────────────────────
    async def async_bulk_cache_refresh(self, tasks):
        """
        Runs a list of async tasks concurrently.
        Each task must be a coroutine.
        """
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                self.logger.log_error(f"❌ Async cache refresh error: {result}")
            else:
                self.logger.log_info(f"✅ Async cache refresh success: {result}")

    # ──────────────────────────────────────────────────────────────────────────────
    # 🎯 Single-Instance Cache Triggers
    # ──────────────────────────────────────────────────────────────────────────────
    @timeit("warm_specific_instance")
    def warm_specific_instance(self, instance, task_function):
        """
        Synchronously warms cache for a single instance using task_function.
        """
        try:
            result = task_function(instance)
            self.logger.log_info(f"✅ Specific cache warm completed for: {instance}")
            return result
        except Exception as e:
            self.logger.log_error(f"❌ Specific cache warm failed for {instance}: {e}")
            return None

    async def async_warm_instance(self, instance, async_task_function):
        """
        Asynchronously warms cache for a single instance using async_task_function.
        """
        try:
            result = await async_task_function(instance)
            self.logger.log_info(f"✅ Async cache warm completed for: {instance}")
            return result
        except Exception as e:
            self.logger.log_error(f"❌ Async cache warm failed for {instance}: {e}")
            return None

    # ──────────────────────────────────────────────────────────────────────────────
    # 📦 Summary Caching
    # ──────────────────────────────────────────────────────────────────────────────
    @timeit("cache_orchestration_summary")
    def cache_orchestration_summary(self, instance, summary, compressed=True):
        """
        Stores a JSON-compatible orchestration summary to cache with optional compression.
        """
        try:
            self.global_cache.set(f"radarr.orchestration.summary.{instance}", summary, compressed=compressed)
            self.logger.log_info(f"📦 Cached orchestration summary for {instance}")
        except Exception as e:
            self.logger.log_error(f"❌ Failed to cache orchestration summary for {instance}: {e}")
