# SonarrOrchestrationCacheManager

**File** — `scripts/managers/services/sonarr/orchestration/cache.py`
**One-liner** — A small parallel-execution helper that runs cache-warming tasks across Sonarr instances using a bounded thread pool or asyncio.

## What it does (for a senior Python engineer)

`SonarrOrchestrationCacheManager(BaseManager, ComponentManagerMixin)`. `parent_name` is derived in `__init__` from the class name (`"SonarrOrchestrationCache"`). It sets up a "dual cache" (`self.sonarr_cache` from kwargs/parent, `self.global_cache` from arg/parent), resolves `sonarr_api`/`logger`/`manager`/`dry_run` from the injected parent, and raises `ValueError` if it ends up without a logger.

It loads **no** submanagers. It is a utility executor, not a tree node with children.

Key public methods:
- `warm_instance_caches(instances, cache_tasks)` — runs warming tasks in parallel via `concurrent.futures.ThreadPoolExecutor`. The pool is bounded: `max_workers = min(8, max(1, len(cache_tasks)))`. `cache_tasks` is a `{instance: task_function}` dict; each `task_function` is submitted with its `instance` argument; results are logged as completed, exceptions logged as errors. (The `instances` parameter is accepted but the tasks dict drives the work.)
- `async_bulk_cache_refresh(tasks)` — `await asyncio.gather(*tasks, return_exceptions=True)` over a list of coroutines, logging each success/exception.
- `warm_specific_instance(instance, task_function)` — runs one sync task for one instance, returns its result (or `None` on error).
- `async_warm_instance(instance, async_task_function)` — awaits one coroutine for one instance, returns result (or `None` on error).

FETCH / CACHE: it is the *driver* for CACHE warmups (the supplied task functions do the actual fetch+cache). It performs no APPLY.

External API endpoints: none directly (the injected task functions hit Sonarr).

Config keys: none read directly.

global_cache / Parquet keys: none read/written directly; the caller's task functions own the keys.

dry_run: captured (`kwargs.get("dry_run", manager.dry_run)`) but not used to gate anything here (no mutating APPLY).

Concurrency/threading: **this is the concurrency primitive of the orchestration layer.** Bounded thread pool (≤ 8 workers) for sync tasks; `asyncio.gather` for coroutine batches. No shared mutable state across tasks beyond logging, so thread-safety is the caller's responsibility for whatever the task functions touch.

## How it functions

Lifecycle: `__init__` wires deps and validates the logger; there is no `run()`. Callers invoke one of the four methods directly, passing in the task callables they want parallelised.

Internally `warm_instance_caches` submits all tasks up front, then drains them with `as_completed`, so per-task failures are isolated and logged without cancelling siblings. The async variants mirror this with `return_exceptions=True`.

Brain delegation: none.

## Criteria & examples

The only "rule" is the worker bound: `max_workers = min(8, max(1, len(cache_tasks)))`. Worked example: warming 3 instances → `min(8, 3) = 3` workers spun up; warming 20 instances → capped at `8` workers so a large multi-instance setup can't fork an unbounded thread storm. With an empty task dict it would be `max(1, 0) = 1` worker (no tasks to run).

## In plain English

This is the "do these chores at the same time" helper. Imagine you have several Sonarr libraries (maybe one for cartoons, one for everything else) and you want to pre-load each one's data so the app feels snappy later. Instead of warming them one at a time, this hands the chores out to up to eight workers at once, then collects the results and notes anyone who tripped. It's the muscle the rest of the orchestration layer borrows whenever a job can be split across instances.

## Interactions

- **Parent manager:** the Sonarr orchestration tree (constructed by `SonarrOrchestrationManager` as its `cache` child).
- **Submanagers:** none.
- **Collaborators:** whoever calls it supplies the warming task callables (which in turn touch `sonarr_api` and `sonarr_cache`/`global_cache`).
- **Brain modules:** none.
