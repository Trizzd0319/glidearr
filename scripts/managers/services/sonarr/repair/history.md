# SonarrRepairHistoryManager

**File** — `scripts/managers/services/sonarr/repair/history.py`
**One-liner** — Reconciles Sonarr's grab/import history by diffing the live history against the cache to spot missing entries; the actual history rebuild is a stub for future Plex/Tautulli integration.

## What it does (for a senior Python engineer)

`SonarrRepairHistoryManager(BaseManager, ComponentManagerMixin)` is a leaf repair sub-manager under `SonarrRepairManager`. It performs **FETCH** (live history) + cache read and reports gaps; the rebuild path is explicitly a stub (no APPLY).

- **Parent:** `self.parent_name = "SonarrRepair"`. Constructed by `SonarrRepairManager` (non-critical).
- **Deps:** `sonarr_api` from `sonarr_api`/`api` kwargs or the registered parent's `api` attr; `instance_manager` from kwarg or parent; `manager` and `dry_run` (from the manager). Raises `ValueError` if API or instance manager cannot be resolved.
- **Loads submanagers:** none.

Public methods:

- **`reconcile_history_gaps(instance_name)`** — resolves the instance, reads live history via `…[resolved].history.all()`, reads the cached history at `global_cache.format_cache_key("sonarr.history", instance=resolved)` (with `fallback=[]`), and computes `cached_ids - live_ids` (entries present in cache but missing live). Logs the count and each missing id, or a "no gaps" line. Returns `None`. (Note the diff direction: it surfaces entries the cache has but the live API no longer reports.)
- **`rebuild_history_for_tvdb(instance_name, tvdb_id)`** — resolves the instance and logs intent; the rebuild itself is a documented stub (`📌 Stub: Rebuild logic not yet implemented`). Returns `None`.

- API endpoints touched: `history.all` (per-instance client).
- Config keys read: none.
- global_cache keys: reads `sonarr.history` formatted with `instance=resolved` (a dotted literal key passed to `format_cache_key`, not a `CacheKeyPaths` constant). No keys written.
- FETCH / CACHE / APPLY: **FETCH** + cache read; no CACHE write, no APPLY (rebuild stubbed).
- dry_run: captured but irrelevant (no mutation occurs).
- Singleton/threading: standard `BaseManager` singleton; no threading.

## How it functions

Lifecycle: `__init__` sets `parent_name`, calls `super().__init__`, `self.register()`, resolves API + instance manager (raises if missing), logs an init line. `reconcile_history_gaps` is a diagnostic set-difference between cached and live history ids. `rebuild_history_for_tvdb` is a placeholder awaiting Plex/Tautulli integration. No `machine_learning` brain module is involved.

## Criteria & examples

- **History gap rule:** `missing = cached_ids - live_ids`. Example: cache holds history entry ids `{1001, 1002, 1003}`, live history reports `{1002, 1003}` → `missing = {1001}` → logs `⚠️ 1 missing history entries found` and `- Missing entry ID: 1001`. If the sets match, logs `✅ No history gaps detected`.
- **Rebuild stub:** `rebuild_history_for_tvdb("sonarr", 81189)` logs the intent and the "not yet implemented" stub line; no data changes.

## In plain English

Sonarr keeps a logbook of every time it grabbed or imported an episode. This specialist compares the app's saved copy of that logbook against the server's current logbook and points out entries the saved copy has that the server no longer shows — a sign the record drifted. The "rebuild the logbook from scratch using Plex/Tautulli watch records" feature is sketched out but not actually built yet, so for now this is purely a "here are the gaps" reporter.

## Interactions

- **Parent manager:** `SonarrRepairManager`.
- **Siblings:** the other `SonarrRepair*Manager` specialists.
- **Services:** the Sonarr per-instance API clients (`sonarr_api`), `instance_manager`, and `global_cache` (history key). The rebuild stub anticipates future Plex/Tautulli history integration.
- **Brain modules:** none.
