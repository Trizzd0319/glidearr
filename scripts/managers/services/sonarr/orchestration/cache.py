import asyncio
import concurrent.futures

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin


class SonarrOrchestrationCacheManager(BaseManager, ComponentManagerMixin):
    """
    Manages orchestration of cache warmups and parallel tasks for Sonarr.
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = self.__class__.__name__.replace("Manager", "")
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

        # 🔧 Dual cache setup
        manager = kwargs.get("manager") or {}
        self.sonarr_cache = kwargs.get("sonarr_cache") or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)

        self.register()

        parent = manager or self.registry.get("manager", self.parent_name)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(parent, "sonarr_api", None)
        self.logger = self.logger or getattr(parent, "logger", None)
        self.manager = manager or getattr(parent, "manager", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        if not self.logger:
            raise ValueError(f"❌ {self.parent_name} could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    def warm_instance_caches(self, instances, cache_tasks):
        """
        Runs warming tasks in parallel for multiple instances using a thread pool.
        :param instances: list of instance names
        :param cache_tasks: dict of {instance: task_function}
        """
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

    async def async_bulk_cache_refresh(self, tasks):
        """
        Runs a list of async tasks concurrently, logs results.
        :param tasks: list of coroutine tasks
        """
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                self.logger.log_error(f"❌ Async cache refresh error: {result}")
            else:
                self.logger.log_info(f"✅ Async cache refresh success: {result}")

    def warm_specific_instance(self, instance, task_function):
        """
        Warms cache for a single instance using a provided sync task function.
        :param instance: name of the instance
        :param task_function: sync function taking instance as arg
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
        Asynchronously warms cache for a single instance using a coroutine.
        :param instance: name of the instance
        :param async_task_function: coroutine taking instance as arg
        """
        try:
            result = await async_task_function(instance)
            self.logger.log_info(f"✅ Async cache warm completed for: {instance}")
            return result
        except Exception as e:
            self.logger.log_error(f"❌ Async cache warm failed for {instance}: {e}")
            return None
