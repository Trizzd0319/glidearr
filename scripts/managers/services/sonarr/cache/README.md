# SonarrCacheManager

- **File** — `scripts/managers/services/sonarr/cache/__init__.py`
- **One-liner** — The Sonarr cache-layer hub: a thin orchestrator that loads and wires every Sonarr cache submanager (series, episodes, episode-files, history, quality, tags, monitoring, instances, orchestration) and proxies the shared `GlobalCacheManager` API.

## What it does (for a senior Python engineer)

`SonarrCacheManager(BaseManager, ComponentManagerMixin)` is the parent node for all `scripts/managers/services/sonarr/cache/*.py` submanagers. It is constructed by `SonarrManager` and stored as `sonarr_cache`.

Responsibilities:
- **Submanager loading.** In `__init__` it builds an `all_component_classes` map and loads each via `split_components(...)` (the `component_splitter` utility) rather than the usual `load_components(...)`. The map is: `episodes`, `history`, `instance`, `monitoring`, `orchestration`, `quality`, `series`, `tags`. Every entry is treated as critical (`critical_keys = set(all_component_classes.keys())`). Each loaded instance is attached as an attribute on `self` (e.g. `self.series`, `self.episodes`) and gets a registry flag `sonarr.cache.<name>_initialized`.
- **Manual `episode_files` load.** `SonarrCacheEpisodeFilesManager` is deliberately excluded from the component map and instantiated by hand near the end of `__init__`. The inline comment explains why: `split_components` temp-instantiates every non-critical entry to inspect `parent_name`, which would double-init `episode_files` and could silently drop it if its `parent_name` did not match the magic string `"SonarrCacheManager"`. On failure it sets `self.episode_files = None` and disables enrichment rather than aborting.
- **Shared dependency injection.** It assembles `init_args` (logger, config, global_cache, validator, registry, `manager=self`, `sonarr_cache=self`, `dry_run`, and `sonarr_api`) and passes them to every child. Note it passes `sonarr_api` straight through (possibly `None`) and explicitly does **not** pass `self` as an API proxy — children must not treat `SonarrCacheManager` as the API.
- **`dry_run` capture.** `self.dry_run` is set from `kwargs` **before** `init_args` is built, so children reading `getattr(manager, "dry_run")` get the right value (see the project's "dry_run propagation footgun" note).
- **Cache proxy utilities.** It forwards a set of `GlobalCacheManager` methods so children can call `sonarr_cache.get(...)`, `.set(...)`, `.get_or_generate_cache(...)`, `.delete(...)`, `.exists(...)`, `.format_cache_key(...)`, `.build_cache_path(...)`, `.deduplicate_entries(...)`, `.update_timestamp(...)`, `.set_with_pretty_output(...)`, and exposes `cache_root` as a property.
- **`initialize_cache_structure(include_optionals=False)`.** Seeds a fixed tree of empty `{"meta": {}, "data": []}` JSON cache files for every instance returned by `self.instance.get_all_instance_names()`. Categories include `series`, `episodes`, `monitoring`, `quality`, `repair`, `sync`, `storage`, `cache`, `instances`, `history`, `tags`, `orchestration` (and optionally `metadata`/`errors`/`stats`/`pipeline`). It skips keys that already exist and logs `📦 Sonarr cache structure initialized (N created, M existed)`.

FETCH / CACHE / APPLY: this class is **CACHE-only** (it is a loader + cache-key proxy). The real FETCH/CACHE/APPLY work is delegated to its children. No external API endpoints are touched directly here.

Singleton / concurrency: `BaseManager` is a process-wide singleton; the `if getattr(self, "_initialized", False): return` guard at the top of `__init__` makes re-construction a no-op.

## How it functions

Lifecycle: `SonarrManager` constructs this with `sonarr_api`. `__init__` → `super().__init__` (BaseManager injects shared deps + auto-links parent) → store `sonarr_api`/`dry_run`/`load_summary` → `split_components` partitions the map into critical/non-critical → loop-instantiate each, set attributes and registry flags → manual `episode_files` init → set `all_components_loaded` and the `sonarr.cache_manager_initialized` flag → `log_filtered_component_summary(...)` prints the one-line load summary.

`initialize_cache_structure` is a separate explicit call (not part of `__init__`) used to pre-seed the empty cache scaffolding before the data-pull phases run.

No decision is delegated to a `machine_learning` brain module from this file directly; its children do that.

## Criteria & examples

- All eight map components are critical — if any fails, `all_critical_loaded` becomes False and `sonarr.cache_manager_initialized` is set False. `episode_files` failing does **not** flip that flag (it is loaded outside the critical loop) but does set `sonarr.cache.episode_files_initialized = False`.
- `initialize_cache_structure`: with two instances `default` and `4k`, the `series` category (subtypes `retrieval`, `sync`, `quality`, `monitoring`) produces keys like `sonarr/default/series_retrieval.json` … `sonarr/4k/series_monitoring.json`. A category with a single empty subtype (e.g. `history: [""]`) yields `sonarr/default/history.json`.

## In plain English

Think of this as the front desk of a film archive's catalog department. It does not itself look anything up; it hires the specialist clerks — the series clerk, the episode clerk, the "what did people actually watch" clerk, the quality clerk, the tags clerk — gives each one the same office supplies (logger, config, the shared filing cabinet), and posts a board saying who showed up for work. It also hands everyone a shortcut to the shared filing cabinet so they do not each have to walk to it. When asked, it can also lay out a fresh set of empty folders so the clerks have somewhere to file things.

## Interactions

- **Parent manager:** `SonarrManager` (constructs and holds this as `sonarr_cache`).
- **Sibling submanagers it loads:** `SonarrCacheSeriesManager` (`series`), `SonarrCacheEpisodesManager` (`episodes`), `SonarrCacheEpisodeFilesManager` (`episode_files`, manually loaded), `SonarrCacheHistoryManager` (`history`), `SonarrCacheInstanceManager` (`instance`), `SonarrCacheMonitoringManager` (`monitoring`), `SonarrCacheQualityManager` (`quality`), `SonarrCacheTagManager` (`tags`), and `SonarrOrchestrationCacheManager` (`orchestration`, defined in the sibling `orchestration/` subdirectory — out of scope here).
- **Shared services:** `GlobalCacheManager` (proxied), `RegistryManager` (component flags), the `sonarr_api` (`SonarrInstanceManager`) passed down to children.
