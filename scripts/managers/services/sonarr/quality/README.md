# SonarrQualityManager

- **File** — `scripts/managers/services/sonarr/quality/__init__.py`
- **One-liner** — The Sonarr "quality" sub-tree orchestrator: it loads and owns the four quality submanagers (adjustments, custom formats, file sizes, selector) and exposes them as attributes on itself.

## What it does (for a senior Python engineer)

`SonarrQualityManager(BaseManager, ComponentManagerMixin)` is a coordinator with no business logic of its own. Its entire job happens in `__init__`: it constructs the four child managers and registers their init status.

Responsibilities:
- Declares `parent_name = "SonarrManager"` at class level, then in `__init__` overwrites `self.parent_name = __class__.__name__` (`"SonarrQualityManager"`), so its own children look it up under that name in the registry.
- Requires a `key_builder` (raises `ValueError` if missing) — children use it to format cache keys.
- Resolves dual caches: `self.sonarr_cache` (from the `cache_manager` kwarg) and `self.global_cache`.
- Builds a `component_map` of four classes and treats ALL of them as critical:
  - `adjustments` → `SonarrQualityAdjustmentManager`
  - `custom_formats` → `SonarrQualityCustomFormatsManager`
  - `file_sizes` → `SonarrQualityFileSizesManager`
  - `selector` → `SonarrQualitySelectorManager`

Position in the manager tree: child of **SonarrManager**; parent of the four submanagers above. (Note: in `compare_profiles_for_series` / `get_best_quality_profile_ai` the selector calls back through `self.manager` for `get_quality_profiles`, `get_custom_format_scores`, `get_default_quality_profile`, and `ml_manager` — i.e. it expects its `manager` reference to be the broader Sonarr quality/manager surface, not strictly this class.)

FETCH / CACHE / APPLY: none directly — it is a pure loader/orchestrator. Its children do the FETCH/CACHE/APPLY.

External API endpoints touched: none directly.

Config keys read: none directly (children read their own).

global_cache / Parquet keys: none directly.

dry_run: reads `kwargs["dry_run"]` (default `False`) and threads it into every child's `init_kwargs`. It performs no mutating work itself, so dry_run only matters via the children.

Singleton / concurrency: like all `BaseManager`s it is a process-wide singleton cached by `(class, singleton_key)`. No threading of its own.

## How it functions

Lifecycle is init-only:
1. `super().__init__(...)` wires the shared deps (logger/config/global_cache/validator/registry) and auto-links to the parent.
2. `self.register()` self-registers under the registry "manager" category.
3. It does NOT use `ComponentManagerMixin.load_components`. Instead it uses the `split_components(...)` helper (`scripts/support/utilities/managers/component_splitter.py`) to split the four classes into `critical` vs `noncritical` buckets given `critical_keys = {"adjustments", "custom_formats", "file_sizes", "selector"}` (so all four are critical and `noncritical_components` is empty). `split_components` pre-bakes each entry's `init_kwargs` (logger, config, global_cache, validator, registry, `manager=self`, `sonarr_api`, `cache_manager=self.sonarr_cache`, `key_builder`, `dry_run`).
4. Two loops instantiate each class with its prepared `init_kwargs`, `setattr(self, name, instance)`, and set a per-child registry flag `sonarr.quality.<name>_initialized` (True on success, False on exception). A failure in any critical child flips `all_critical_loaded` to False but does not abort the others.
5. Sets `self.all_components_loaded` and the registry flag `sonarr.quality_manager_initialized` to `all_critical_loaded`.
6. Calls `log_filtered_component_summary(...)` to emit one summary line.

No decision is delegated to a `machine_learning` brain module by this file directly (its child `selector` is the one that reaches into `ml_manager`).

## Criteria & examples

The only rule here is the critical/noncritical split. Because `critical_keys` contains all four component names, every child is critical. Concrete example: if `SonarrQualitySelectorManager` raises during construction, `self.load_summary["selector"]` becomes `"❌ Failed: <err>"`, the flag `sonarr.quality.selector_initialized` is set False, `all_critical_loaded` becomes False, and therefore `sonarr.quality_manager_initialized` is set False — but `adjustments`, `custom_formats`, and `file_sizes` still get constructed and attached.

## In plain English

Think of this class as the manager of a "video quality" department with four specialists: one who tunes settings (adjustments), one who keeps a list of preferred file traits (custom formats), one who checks whether episode files are too big or too small (file sizes), and one who picks the best quality recipe per show (selector). This class's only job on day one is to hire all four, give each the same office supplies (logger, config, caches), and post a note on the board saying which ones showed up for work. It doesn't make any quality decisions itself — it just makes sure the team exists and is reachable.

## Interactions

- **Parent manager:** `SonarrManager`.
- **Sibling/child submanagers it loads:** `SonarrQualityAdjustmentManager`, `SonarrQualityCustomFormatsManager`, `SonarrQualityFileSizesManager`, `SonarrQualitySelectorManager`.
- **Helpers:** `split_components` (component bucketing), `log_filtered_component_summary` / `LoggerManager`, `RegistryManager` (flags).
- **Brain modules:** none directly; the `selector` child is the one that talks to `ml_manager`.
