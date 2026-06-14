# SonarrRepairManager

**File** — `scripts/managers/services/sonarr/repair/__init__.py`
**One-liner** — The Sonarr "repair" sub-tree orchestrator: it loads and owns the fifteen specialist repair sub-managers that audit and fix inconsistencies in a Sonarr library (paths, tags, monitoring, quality, cache, orphans, anomalies, etc.).

## What it does (for a senior Python engineer)

`SonarrRepairManager(BaseManager, ComponentManagerMixin)` is the package facade for the Sonarr repair toolkit. It is constructed by `SonarrManager` (its declared `parent_name = "SonarrManager"`) and, in turn, instantiates every repair specialist as an attribute on itself.

Unlike most managers in the tree, it does **not** call `ComponentManagerMixin.load_components`. Instead its `__init__` builds the component map by hand and uses `split_components` (from `scripts/support/utilities/managers/component_splitter.py`) to partition the components into critical vs non-critical, then instantiates each class directly in a `try/except` loop. This is a deliberate workaround documented in the source comments: `BaseManager` overwrites `self.parent_name` with the caller's `init_args` value ("SonarrManager"), so the code passes `parent_name_match=self.__class__.__name__` ("SonarrRepairManager") and an explicit `parent_name` kwarg to each child so the splitter matches them correctly.

- **Component map (15 children):** `anomaly` → `SonarrRepairAnomalyManager`, `repair_cache` → `SonarrRepairCacheManager`, `episodes` → `SonarrRepairEpisodesManager`, `file` → `SonarrRepairFileManager`, `filepaths` → `SonarrRepairFilepathsManager`, `history` → `SonarrRepairHistoryManager`, `instance` → `SonarrRepairInstanceManager` (defined in the `instance/` subpackage — documented separately), `metadata` → `SonarrRepairMetadataManager`, `monitoring` → `SonarrRepairMonitoringManager`, `orphans` → `SonarrRepairOrphansManager`, `quality` → `SonarrRepairQualityManager`, `series` → `SonarrRepairSeriesManager`, `storage` → `SonarrRepairStorageManager`, `tags` → `SonarrRepairTagsManager`, `validator` → `SonarrRepairValidatorManager`.
- **Critical keys:** `{cache, filepaths, instance, monitoring, storage, validator}`. Note the literal string `"cache"` in `critical_keys` does not match the component key `"repair_cache"`, so `repair_cache` is partitioned as **non-critical** in practice. If a critical child fails, `all_critical_loaded` flips to `False`.
- **Dependency injection:** it assembles `repair_init_kwargs` once (logger/config/global_cache/validator/registry/manager=self, plus `sonarr_api` and `instance_manager` resolved from the incoming kwargs or the parent manager, plus the explicit `parent_name`) and passes the same dict to every child constructor.
- **Registry flags:** per child it sets `sonarr.repair.<name>_initialized` (True/False), and at the end sets `sonarr.repair_manager_initialized` to `all_critical_loaded`.
- **No public run method.** It exposes the children as attributes and a `self.load_summary` dict ("✅ Loaded" / "❌ Failed: …" per child). Callers reach the actual repair work through e.g. `self.storage.repair_storage_paths()`.
- FETCH / CACHE / APPLY: none directly — it is a loader/aggregator. Its children perform FETCH/CACHE/APPLY.
- Config keys read: none directly.
- global_cache keys: none directly.
- dry_run: not consulted here; each child resolves its own `dry_run` from the manager chain.
- Singleton/threading: it is a `BaseManager` singleton (cached in `BaseManager._instances`). Construction is sequential and single-threaded.

## How it functions

Lifecycle: `__init__` → `super().__init__` (injects shared deps, auto-links parent) → `self.register()` → build `all_component_classes` and `critical_keys` → `split_components(...)` → instantiate critical children, then non-critical children, setting registry flags and recording outcomes in `load_summary` → set `all_components_loaded` / `sonarr.repair_manager_initialized` → `log_filtered_component_summary(...)` emits the one-line load summary.

Notable internal behavior: each child is wrapped in `try/except Exception`; a failing non-critical child is logged but does not abort the tree, while a failing critical child sets `all_critical_loaded = False` (which gates the `sonarr.repair_manager_initialized` flag). No decision is delegated to a `machine_learning` brain module here.

## Criteria & examples

- A child listed in `critical_keys` that raises during construction sets `all_critical_loaded = False`. Example: if `SonarrRepairValidatorManager` raises `ValueError("Missing required API…")` because `sonarr_api` could not be resolved, the `validator` flag `sonarr.repair.validator_initialized` is set `False`, `load_summary["validator"]` becomes `"❌ Failed: …"`, and the package flag `sonarr.repair_manager_initialized` is `False`.
- A non-critical child failing leaves `all_critical_loaded` untouched. Example: if `SonarrRepairAnomalyManager` fails, `sonarr.repair_manager_initialized` can still be `True`.

## In plain English

Think of this as the front desk of a TV-library repair shop. When the shop opens, the manager calls in fifteen specialists — one who fixes folder paths, one who tidies tags, one who fixes which shows are being recorded, one who clears out stale notes, and so on. The manager keeps a sign-in sheet noting who showed up ("✅ Loaded") and who didn't ("❌ Failed"). A few specialists are "essential" — if any of those don't show up, the manager marks the whole shop as not fully open for the day. The front desk doesn't fix anything itself; it just makes sure the right specialists are present so that when someone says "go fix the folder paths," the right expert is on hand.

## Interactions

- **Parent manager:** `SonarrManager` (the top-level Sonarr service manager).
- **Sibling/child submanagers (loaded here):** the fifteen `SonarrRepair*Manager` classes listed above; each is reachable as an attribute (`self.storage`, `self.tags`, `self.validator`, …).
- **Brain modules:** none; this orchestrator delegates no value judgements to `machine_learning`.
- **Other services:** indirectly the Sonarr HTTP API and instance manager, which it threads into the children via `repair_init_kwargs`.
