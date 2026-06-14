# RadarrOrchestrationCacheManager

- **File** — `scripts/managers/services/radarr/cache/orchestration.py`
- **One-liner** — Concurrency utility for the cache tree: runs cache-warmup tasks in parallel (thread pool or asyncio) and persists orchestration summaries.

## What it does (for a senior Python engineer)

`RadarrOrchestrationCacheManager(BaseManager, ComponentManagerMixin)` is a generic task-runner / fan-out helper. It does no Radarr FETCH itself; it CACHEs one thing (an orchestration summary) and otherwise just executes caller-supplied callables/coroutines. The tasks it runs are what touch the API.

Where it sits in the tree:
- **Parent**: `RadarrCacheManager` (`parent_name = "RadarrCacheManager"`).
- **Submanagers**: none.

Public methods:
- `warm_instance_caches(instances, cache_tasks)` — `cache_tasks` must be a `dict` of `{instance: callable}` where each callable accepts `(instance)`. Submits each to a bounded `ThreadPoolExecutor` (`max_workers = min(8, max(1, len(cache_tasks)))`), then logs success/failure as each future completes. Type-guards `cache_tasks`: non-dict → error log + return.
- `async_bulk_cache_refresh(tasks)` (coroutine) — `await asyncio.gather(*tasks, return_exceptions=True)`; logs each result or exception.
- `warm_specific_instance(instance, task_function)` — synchronously runs `task_function(instance)` inside a try/except; returns the result or `None`.
- `async_warm_instance(instance, async_task_function)` (coroutine) — awaits `async_task_function(instance)`; returns result or `None`.
- `cache_orchestration_summary(instance, summary, compressed=True)` — CACHE the supplied summary under `radarr.orchestration.summary.<instance>`.

External API endpoints: none directly (the injected tasks do the I/O).
Config keys read: none.
Global_cache keys written: `radarr.orchestration.summary.<instance>`.

`dry_run`: captured but unused — this manager only schedules work and writes a summary cache; nothing destructive.

Concurrency/threading notes:
- `warm_instance_caches` uses a `ThreadPoolExecutor` capped at 8 workers (comment: prevents unbounded worker spawn). The iteration key is named `instance` but it iterates `cache_tasks.items()` (so the dict key is passed as the per-task `instance` arg).
- `async_bulk_cache_refresh` / `async_warm_instance` are asyncio coroutines and must be awaited.
- `@timeit(...)` wraps `warm_instance_caches`, `warm_specific_instance`, and `cache_orchestration_summary`.

## How it functions

`__init__` does BaseManager wiring, `self.register()`, then resolves `radarr_api`, `instance_manager`, `manager`, and `dry_run` from kwargs-or-parent. There is no single `run()`; callers hand it task callables/coroutines and pick the parallel (thread), async, or single-instance entry point. No machine_learning delegation.

## Criteria & examples

- Worker-bound rule: `max_workers = min(8, max(1, len(cache_tasks)))`. Example: 3 tasks → 3 workers; 20 tasks → 8 workers; an (atypical) empty dict → `max(1, 0) = 1` worker but nothing to run.
- Input guard: `warm_instance_caches(instances, [fn1, fn2])` (a list, not a dict) logs `❌ Invalid cache_tasks type: expected dict, got list` and returns without running anything.

## In plain English

When several caches need refreshing, doing them one after another is slow. This is the "send everyone off to do their jobs at once" coordinator: hand it a to-do list of refresh jobs and it runs up to eight of them simultaneously, reporting which finished and which failed. It can also stash a short report card summarising how a refresh round went.

## Interactions

- **Parent**: `RadarrCacheManager`.
- **Siblings**: typically used to parallelise the refresh methods of the other cache submanagers (e.g. `RadarrInstanceCacheManager.refresh_*`, `RadarrQualityCacheManager.refresh_*`), which are passed in as the task callables.
- **Services**: none directly (the injected tasks call `radarr_api`).
- **Brain modules**: none.
