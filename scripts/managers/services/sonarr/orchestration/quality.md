# SonarrOrchestrationQualityManager

**File** — `scripts/managers/services/sonarr/orchestration/quality.py`
**One-liner** — Loads and exposes the four Sonarr quality submanagers (adjustments, custom formats, file sizes, selector) as a single orchestration façade.

## What it does (for a senior Python engineer)

`SonarrOrchestrationQualityManager(BaseManager, ComponentManagerMixin)` with `parent_name` set to `"SonarrOrchestration"` (class attr) then overwritten in `__init__` to the class name. It instantiates four leaf quality managers and attaches each as an attribute.

Components loaded:
- `adjustments` → `SonarrQualityAdjustmentManager`
- `custom_formats` → `SonarrQualityCustomFormatsManager`
- `file_sizes` → `SonarrQualityFileSizesManager`
- `selector` → `SonarrQualitySelectorManager`

All four are in `critical_keys`, so all are treated as critical components. `key_builder` is **required** — `__init__` raises `ValueError` if it is not provided.

The component init bundle injected into each child includes: `logger`, `config`, `global_cache`, `validator`, `registry`, `manager=self`, `sonarr_api`, `cache_manager` (= `self.sonarr_cache`), `key_builder`, `dry_run`.

It uses `split_components(...)` (from `support/utilities/managers/component_splitter.py`) to partition the four classes into critical vs non-critical given `critical_keys` and `parent_name_match`. (Because all four keys are critical, the non-critical loop is normally empty.) Each component instantiated successfully sets a registry flag `sonarr.orchestration.quality.<name>_initialized = True` (else `False` on failure with `❌ Failed: <e>` in `load_summary`). An overall flag `sonarr.orchestration.quality_manager_initialized` is set to `all_critical_loaded`, and a filtered component summary is logged via `log_filtered_component_summary`.

Key public methods (pure getters):
- `get_adjustment_manager()` → `self.adjustments`
- `get_custom_formats_manager()` → `self.custom_formats`
- `get_file_sizes_manager()` → `self.file_sizes`
- `get_selector_manager()` → `self.selector`

FETCH / CACHE / APPLY: none here directly — this is a loader/accessor. The leaf quality managers perform the actual quality-profile / custom-format APPLY (PUTs) and file-size CACHE.

External API endpoints: none directly.

Config keys: none read directly (it forwards `config` to children).

global_cache / Parquet keys: none read/written directly; the `selector`/`file_sizes` children use `key_builder` to construct their own cache keys.

dry_run: `self.dry_run = kwargs.get("dry_run", False)`, forwarded to all children. No APPLY of its own to gate.

Singleton/concurrency: BaseManager singleton; construction is sequential, no threading.

## How it functions

Lifecycle: `__init__` → `super().__init__` → `register()` → resolve `key_builder`/`dry_run`/caches → enforce `key_builder` presence → define `all_component_classes` + `critical_keys` → `split_components(...)` → loop the critical map instantiating each class and setting per-component registry flags → loop the (empty) non-critical map → set `self.all_components_loaded` and the aggregate registry flag → `log_filtered_component_summary`.

There is no `run()` entry method; this manager is consumed by other orchestrators that call its getters to reach a specific quality leaf.

Brain delegation: none in this file. (Quality *decisions* such as which profile to pick under space pressure live further down / in the brain, reached through the `selector`/`adjustments` leaves.)

## Criteria & examples

The only gate is the hard `key_builder` requirement and the critical-load accounting. Example: if `SonarrQualitySelectorManager(**kwargs)` raises during construction, `registry` flag `sonarr.orchestration.quality.selector_initialized` is set `False`, `load_summary["selector"] = "❌ Failed: <error>"`, `all_critical_loaded` becomes `False`, and the aggregate flag `sonarr.orchestration.quality_manager_initialized` is set `False` — signalling downstream callers that quality orchestration is degraded.

## In plain English

Picture the "picture-quality desk" for your TV shows, staffed by four specialists: one who decides how big a good copy of an episode should be, one who knows the special tagging rules (custom formats), one who tweaks the quality settings, and one who picks the right quality tier for a given show. This manager's whole job is to hire those four and hand each new visitor a direct phone line to the right specialist. It insists on having its label-maker (`key_builder`) before it opens, because every specialist needs consistent labels to file their work. If a specialist can't be hired, it raises a flag so the rest of the system knows the picture-quality desk is short-staffed.

## Interactions

- **Parent manager:** constructed by `SonarrOrchestrationManager` as its `quality` child (declared `parent_name` is `"SonarrOrchestration"`).
- **Children it loads:** `SonarrQualityAdjustmentManager`, `SonarrQualityCustomFormatsManager`, `SonarrQualityFileSizesManager`, `SonarrQualitySelectorManager`.
- **Helpers:** `split_components` (component partitioning) and `log_filtered_component_summary` (mixin summary log).
- **Brain modules:** none directly.
