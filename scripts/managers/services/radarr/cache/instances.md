# RadarrInstanceCacheManager

- **File** — `scripts/managers/services/radarr/cache/instances.py`
- **One-liner** — Caches per-instance Radarr health and system metadata, with read accessors, a summary, a bulk refresh, and a purge.

## What it does (for a senior Python engineer)

`RadarrInstanceCacheManager(BaseManager, ComponentManagerMixin)` is a thin instance-health adapter. It performs FETCH (GET `system/status`, `health`), CACHE (writes the two per-instance caches), and a cache-delete (purge). No APPLY to Radarr.

Where it sits in the tree:
- **Parent**: `RadarrCacheManager` (`parent_name = "RadarrCacheManager"`).
- **Submanagers**: none.

Public methods:
- `get_instance_metadata(instance)` — reads `radarr.instance.metadata.<instance>` (default `{}`).
- `get_instance_health(instance)` — reads `radarr.instance.health.<instance>` (default `[]`).
- `refresh_instance_metadata(instance)` — FETCH `GET system/status`; CACHE under `radarr.instance.metadata.<instance>` (`compressed=True`).
- `refresh_instance_health(instance)` — FETCH `GET health`; CACHE under `radarr.instance.health.<instance>` (`compressed=True`).
- `refresh_all_instances(instances)` — loops the given iterable, calling both refreshers per instance.
- `summarize_instance(instance)` — reads the two caches, counts health entries with `type == "error"`, returns `{"name", "version", "health_issues"}` and logs it.
- `purge_instance_cache(instance)` — deletes both `radarr.instance.metadata.<instance>` and `radarr.instance.health.<instance>`.

External API endpoints: `GET system/status`, `GET health`.
Config keys read: none.
Global_cache keys: reads/writes `radarr.instance.metadata.<instance>` and `radarr.instance.health.<instance>`; `purge_instance_cache` deletes both.

`dry_run`: captured but unused; the manager performs no destructive Radarr writes (its only mutation is to its own cache).

Singleton/concurrency: standard `BaseManager` singleton; `refresh_all_instances` is a plain sequential loop (no threading — that is the orchestration manager's job).

## How it functions

`__init__` does BaseManager wiring, `self.register()`, then resolves `radarr_api`, `instance_manager`, `manager`, and `dry_run` from kwargs-or-parent. No `run()` and no `load_components`; callers invoke the helpers. No machine_learning delegation.

## Criteria & examples

- Empty-result guard: both refreshers skip the cache write and log a warning when the API returns falsy. Example: `refresh_instance_health("default")` with `[]` logs `⚠️ No health data for default`.
- `summarize_instance` error count: given cached health `[{"type": "error", ...}, {"type": "warning", ...}]`, `health_issues == 1`; missing `name`/`version` default to `"unknown"`.

## In plain English

Every Radarr server reports two things: a quick "about me" card (its name and version) and a "is anything wrong" health list. This manager keeps copies of both per server, can refresh them all in one pass, can hand back a tidy one-line summary like "MyRadarr v5, 1 health issue," and can wipe a server's cached info when you want to start fresh.

## Interactions

- **Parent**: `RadarrCacheManager`.
- **Siblings**: `RadarrOrchestrationCacheManager` can drive these refreshers in parallel via `warm_instance_caches`.
- **Services**: `radarr_api`.
- **Brain modules**: none.
