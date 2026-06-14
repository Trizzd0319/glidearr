# SonarrRepairCacheManager

**File** — `scripts/managers/services/sonarr/repair/cache.py`
**One-liner** — Clears all cached entries in the `sonarr` namespace (optionally scoped to one instance) to force a clean rebuild of the Sonarr cache.

## What it does (for a senior Python engineer)

`SonarrRepairCacheManager(BaseManager, ComponentManagerMixin)` is a leaf repair sub-manager under `SonarrRepairManager`. It operates on `global_cache` only and performs **CACHE invalidation** (clear keys) — no live Sonarr API calls.

- **Parent:** computes `parent_name` by stripping the trailing "Manager" from its own class name, yielding `"SonarrRepairCache"`. (It first assigns `"SonarrRepair"` then overwrites it.) Constructed by `SonarrRepairManager` under the component key `repair_cache`.
- **Deps:** resolves `logger`, `manager`, `dry_run`, plus a "dual cache" — `sonarr_cache` (the per-service cache, from the `cache_manager` kwarg or parent) and `global_cache`. Hard-requires a logger (raises `ValueError` otherwise).
- **Loads submanagers:** none.

Public method:

- **`repair_all_cache(instance_name=None)`** — enumerates cache keys in the `sonarr` namespace via `global_cache.get_keys(namespace="sonarr", instance=instance_name)`, and clears each with `global_cache.clear(key)` inside a `try/except`. Records successes in `repaired` and failures in `failed` (as `(key, error)` tuples), logs a summary, and returns `{"repaired": [...], "failed": [...]}`.

- API endpoints touched: none.
- Config keys read: none.
- global_cache keys: reads the key list for namespace `sonarr` (optionally filtered by `instance`) and clears each.
- FETCH / CACHE / APPLY: **CACHE** (invalidation) only.
- dry_run: stored on `self.dry_run` but **not consulted** — `repair_all_cache` clears keys regardless of dry-run. (Worth noting: this is the one repair method here that mutates state and does not honor dry-run.)
- Singleton/threading: standard `BaseManager` singleton; no threading.

## How it functions

Lifecycle: `__init__` derives `parent_name` from the class name, calls `super().__init__`, `self.register()`, looks up the registered parent for fallbacks, wires both caches, enforces the logger precondition, logs an init line. `repair_all_cache` is a single sweep: list keys → clear each → tally. There is no rebuild step — clearing the keys forces other managers to regenerate them on next access. No `machine_learning` brain module is involved.

## Criteria & examples

- **Namespace scoping:** only keys returned by `get_keys(namespace="sonarr", instance=instance_name)` are cleared. Example: `repair_all_cache(instance_name="sonarr_4k")` clears only that instance's sonarr keys; `repair_all_cache()` (no arg) clears all sonarr-namespace keys.
- **Failure isolation:** if `clear(key)` raises for one key, that key is recorded in `failed` and the sweep continues. Example summary: `🧾 Cache Repair Summary: 12 repaired, 1 failed`.

## In plain English

This is the "wipe the sticky notes" specialist. When the catalog's quick-reference notes about your Sonarr library have gone stale or wrong, the simplest fix is to throw them all away so they get freshly rewritten the next time someone looks. You can tell it to wipe notes for just one library or for all of them. It then reports how many notes it tore up and whether any refused to come off. (Note: unlike most of the repair crew, this one does the wiping even in practice mode.)

## Interactions

- **Parent manager:** `SonarrRepairManager`.
- **Siblings:** the other `SonarrRepair*Manager` specialists.
- **Services:** `global_cache` (key enumeration + clear); holds a reference to `sonarr_cache` for the dual-cache convention, unused by current logic.
- **Brain modules:** none.
