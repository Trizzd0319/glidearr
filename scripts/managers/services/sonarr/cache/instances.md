# SonarrCacheInstanceManager

- **File** — `scripts/managers/services/sonarr/cache/instances.py`
- **One-liner** — Caches per-instance Sonarr metadata and health, summarizes instance state, and enumerates configured instance names.

## What it does (for a senior Python engineer)

`SonarrCacheInstanceManager(BaseManager, ComponentManagerMixin)` is reachable as `sonarr_cache.instance`.

Public methods:
- `refresh_instance_metadata(instance)` — FETCH `self.sonarr_api.get_instance_metadata(instance)`, CACHE to `sonarr.instance.metadata.{instance}` (dotted key, via `global_cache.set`).
- `get_instance_metadata(instance)` — read that key back (`{}` default).
- `refresh_instance_health(instance)` — FETCH `self.sonarr_api.get_instance_health(instance)`, CACHE to `sonarr.instance.health.{instance}`.
- `get_instance_health(instance)` — read back (`[]` default).
- `refresh_all_instances(instances)` — refresh both metadata and health for each instance in the list.
- `summarize_instance(instance)` — combine cached metadata + health into `{"name", "version", "health_issues"}`, where `health_issues` counts health entries with `type == "error"`; logs the summary.
- `purge_instance_cache(instance)` — delete both cache keys for an instance.
- `get_all_instance_names()` — read `config["sonarr_instances"]` and return its keys **excluding** the literal `"default_instance"` (which is a pointer, not an instance).

FETCH / CACHE / APPLY: **FETCH + CACHE** (no Sonarr writes). External API: `get_instance_metadata`, `get_instance_health`. Config keys: `sonarr_instances` (read, in `get_all_instance_names`). Cache keys (dotted, not path-style): `sonarr.instance.metadata.{instance}`, `sonarr.instance.health.{instance}`.

`dry_run`: captured in `__init__`; not relevant (caching local diagnostics is non-destructive).

`get_all_instance_names()` is the seed used by `SonarrCacheManager.initialize_cache_structure` to iterate every instance when laying out the empty cache tree.

## How it functions

Init sets `parent_name = "SonarrCache"`, wires the dual cache + `sonarr_api`/`logger`/`manager`/`dry_run`, raises without a logger, then registers. No `load_components` (no submanagers). Each method is a single cache read/write or a small combine-and-log. No decision is delegated to a `machine_learning` module.

## Criteria & examples

- `summarize_instance("default")`: if cached health is `[{"type":"warning",...}, {"type":"error",...}]`, the summary reports `health_issues: 1` (only `type=="error"` counts), plus the metadata `name`/`version`.
- `get_all_instance_names()`: a config of `{"default": {...}, "4k": {...}, "default_instance": "default"}` returns `["default", "4k"]` — the `default_instance` pointer is filtered out.

## In plain English

This clerk keeps a quick health card for each of your Sonarr servers — its name, its version, and how many things are currently broken (counting only real errors, not minor warnings). It can refresh those cards, hand back a one-line summary like "Sonarr 4.0.1, 1 health issue", or throw the card away. It also keeps the official roster of which Sonarr servers you actually have configured, ignoring the sticky note that merely says which one is the default.

## Interactions

- **Parent manager:** `SonarrCacheManager` (attached as `instance`).
- **Services:** the `sonarr_api` gateway (`SonarrInstanceManager`) for metadata/health; `global_cache` for the dotted instance cache keys.
- **Consumers:** `SonarrCacheManager.initialize_cache_structure` calls `get_all_instance_names()` to enumerate instances.
- **Brain modules:** none.
