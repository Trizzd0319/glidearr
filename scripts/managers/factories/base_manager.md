# BaseManager

**File** — `scripts/managers/factories/base_manager.py`
**One-liner** — The process-wide singleton base class every Glidearr manager inherits from; it injects the shared dependency set (logger, config, cache, validator, registry), self-registers, and auto-links each manager to its parent in the manager tree.

## What it does (for a senior Python engineer)

`BaseManager` is the root of the manager class hierarchy. Almost every other manager (`Main`, the service managers, `BaseInstanceManager`, `SingletonInitializer`, `GlobalCacheManager`, `RegistryManager`, etc.) subclasses it. It owns four cross-cutting concerns: **singleton identity**, **dependency injection**, **registry self-registration**, and **parent auto-linking**.

It performs none of FETCH / CACHE / APPLY itself — it is pure infrastructure. It holds a reference to `global_cache` so subclasses can read/write the cache, but BaseManager neither calls an HTTP endpoint nor reads/writes a specific cache key of its own. There is no `dry_run` logic at this layer.

### Singleton identity (`__new__`)

- Class attributes `_instances = {}` (the global instance registry) and `_singleton_instances = {}` with a `_singleton_lock` (a `threading.Lock`).
- `__new__(cls, *args, **kwargs)` keys the instance by `(cls, kwargs.get("singleton_key"))`. If that key is absent from `_instances`, it creates and stores a new instance; otherwise it returns the cached one. So two constructions of the same class with the same `singleton_key` (default `None`) yield the *same* object. Note that `__init__` still runs again on the returned instance each time it is constructed (Python always calls `__init__` after `__new__` returns an instance of `cls`).

### Dependency injection (`__init__`)

Signature: `__init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs)`. Decorated with `@timeit("__init__")`.

- `self.name = self.__class__.__name__`.
- `self.logger = logger or LoggerManager()`.
- `self.config = config or ConfigManager(logger=self.logger)`.
- `self.global_cache`, `self.validator` are taken as-passed (may be `None`).
- `self.registry = registry or RegistryManager()`.
- `self.cli_flags = kwargs.get("cli_flags", {})`, `self.timestamp = datetime.now().isoformat()`.
- `self.parent_name = kwargs.get("parent_name") or self._infer_parent_from_path()` (see below).
- Builds `self.dep_versions` = `{"config_version": getattr(self.config, 'version', 'n/a'), "cache_keys": <first 5 memory_cache keys>}` (cache-key preview is skipped for `GlobalCacheManager` to avoid recursion).

### Registration & parent auto-linking (`__init__`, inside try/except)

- `self.registry.register("manager", self.name, self)` — every manager self-registers under the registry **`"manager"`** category keyed by class name.
- Optionally `self.registry.print_tree_view(category="manager")` if `kwargs["print_registry_tree"]` is truthy.
- `self.registry.auto_hot_swap_from_config(self.config.raw_data)` — lets config drive hot-swaps.
- Looks up `parent = self.registry.get("manager", self.parent_name)`. If found, the manager **inherits the parent's deps**: `logger`, `config`, `global_cache` (read from the parent attribute `cache`), `validator`, and sets `self.manager = parent.manager or parent`. This is how the whole subtree ends up sharing one logger/config/cache/validator.
- Any failure here is caught and logged as a warning (non-fatal).

### Deferred parent link (`_resolve_deferred_parent`)

Called unconditionally at the end of `__init__`. If `self.manager` is still unset and a `parent_name` exists, it retries `registry.get("manager", self.parent_name)`; on success it re-inherits `logger`/`config`/`global_cache`/`validator`/`manager` from the parent (here it reads the parent's `global_cache` attribute, not `cache`). This handles the case where the parent manager had not yet been constructed when this child initialized.

### Key public / notable methods

- `prepare(self)` — Loads "critical" subcomponents silently then emits one summary line. Reads `self.critical_keys` (a list, default `[]`); for each name still `None`, calls `self._load_component(name)`; then logs `[Cls] ✅ n/total: name✅ name❌ …` based on `self.load_summary`. No-op when `critical_keys` is empty. Overridden by `BaseInstanceManager.prepare` (which is a no-op) and by component managers.
- `run(self)` — Base **no-op** run; logs a debug line. Orchestration-driven managers override it.
- `_load_component(self, name)` — Late-binds a subcomponent: fetches `registry.get("manager", name)` and, if present, `setattr(self, name, component)`. Warns if the registry or the component is missing.
- `format_cache_key(self, key, instance=None, user=None)` — Template substitution: replaces the literal tokens `<instance>` and `<user>` in a cache-key string (defaulting each to `"default"`).
- `_get_last_timestamp(self, cache_key, instance)` — Reads `global_cache.get(cache_key)` and returns `cached["meta"]["timestamp"]` if present, else `None`.
- `resolve_instance(self, instance)` — Returns the instance if it is a string, else `instance.name`; raises `ValueError` otherwise.
- `_singleton(self, name, cls, **kwargs)` — Double-checked-locked factory: keyed by `(self.__class__.__name__, name)` in the shared `_singleton_instances` dict, guarded by `_singleton_lock`, lazily constructs and caches `cls(**kwargs)`. Returns the cached instance.
- `get_tag_monitor(self)` — Resolves the Sonarr keep-tag monitor `SonarrSyncTagsManager` (the object exposing `is_series_tagged_keep(series_id)` used to protect `keep`-tagged series). Tries the registry first; if absent, lazily constructs it as a `BaseManager` singleton with this manager's context (`sonarr_api`/`instance_manager` pulled from `self` via `getattr`). Returns `None` if it cannot be resolved (e.g. called outside a Sonarr context). Safe to call before caches are warm because the keep-set is populated lazily inside `is_series_tagged_keep`.
- `_infer_parent_from_path(self)` — Heuristic that derives `parent_name` from the class's source-file path: it looks for a `sonarr`/`radarr`/`tautulli`/`trakt` path segment to form a service prefix and combines it with the containing folder name (e.g. a class in `…/sonarr/sync/` → `SonarrSync`; collapses a duplicate like `SonarrSonarr` → `SonarrManager`). Falls back to the class name on error.
- `_preview_cache_keys(self)` — Returns the first 5 keys of `global_cache.memory_cache` (empty for `GlobalCacheManager`).

Vestigial stubs kept for compatibility: `_init_summary_data`, `_register_with_registry`, `_log_init_summary` are all `pass` (their work moved into `__init__` / `_finalize` / `prepare`).

### Threading / singleton notes

- `_instances` (per `(cls, singleton_key)`) and `_singleton_instances` (per `(owner-class, name)`) are **class-level dicts shared across the whole process**. `_singleton_instances` access is guarded by `_singleton_lock`; the `_instances` write in `__new__` is *not* explicitly locked.

## How it functions

Lifecycle of any manager: `__new__` resolves/creates the singleton object → `__init__` injects deps, computes `parent_name`, self-registers under `"manager"`, attempts the parent link (inheriting the parent's shared deps), then runs `_resolve_deferred_parent()` as a safety net. Later, an orchestrator (typically `Main`) calls `prepare()` to materialize critical submanagers and `run()` to do the work (both overridden downstream).

The central design move is **dependency inheritance through the registry**: rather than threading the same logger/config/cache through every constructor by hand, a child registers itself, finds its parent by name, and copies the parent's references. `_infer_parent_from_path` makes that link automatic for managers laid out under a service folder.

This module delegates **no** decisions to a `machine_learning` brain module; it is foundational plumbing beneath every manager (including the thin service adapters that *do* delegate to the brain).

## Criteria & examples

- **Singleton reuse:** Constructing `GlobalCacheManager()` twice with no `singleton_key` produces `inst_key = (GlobalCacheManager, None)`; the second call hits the existing entry in `_instances` and returns the very same object (`id` unchanged), so all managers share one cache.
- **Parent inheritance:** A class defined in `scripts/managers/services/sonarr/sync/tags.py` gets `parent_name` inferred as `SonarrSync`. If a `SonarrSync` manager is already registered, the new manager silently adopts its logger, config, `global_cache` (from the parent's `cache` attr), and validator — no explicit wiring needed.
- **Deferred link:** If `SonarrSyncTagsManager` is built *before* its `SonarrSync` parent exists, the initial parent lookup returns `None`; `_resolve_deferred_parent()` later succeeds once the parent is registered and emits `🔗 Deferred linking: SonarrSyncTagsManager → SonarrSync`.
- **`prepare()` summary:** With `critical_keys = ["orchestration", "selector"]`, if `load_summary = {"orchestration": "✅ ok", "selector": "❌ missing"}`, it logs `[Cls] ⚠️ 1/2: orchestration✅ selector❌` (the `⚠️` because not all were OK).

## In plain English

Think of `BaseManager` as the staffing-and-org-chart office for the whole operation. Every team that joins (the Sonarr team, the Radarr team, the Trakt team) reports in at this office. The office (1) makes sure there's only ever *one* of each team — if someone asks for "the Sonarr team" again, they get the same people, not a duplicate; (2) hands every new team the shared tools everyone uses — the same notepad (logger), the same rulebook (config), the same filing cabinet (cache); and (3) figures out who each team reports to and clips them onto the org chart automatically, even if their boss hadn't shown up yet when they arrived.

For a viewer, none of this is visible — it's the back-office wiring that ensures, say, the team protecting your *keep*-tagged box set of **The Lord of the Rings** is reading the same rulebook and the same library snapshot as the team deciding what to download next, so they never contradict each other.

## Interactions

- **Parent manager:** none — this *is* the base. At runtime each instance discovers its parent via `parent_name` + the registry.
- **Direct subclasses / consumers:** `BaseInstanceManager` (and through it the Radarr/Sonarr instance managers), `SingletonInitializer`, `GlobalCacheManager`, `RegistryManager`, `Main`, and every service manager in the tree.
- **Injected collaborators:** `LoggerManager`, `ConfigManager`, `RegistryManager` (registration + `auto_hot_swap_from_config` + `get`/`set`/`print_tree_view`), `GlobalCacheManager` (held as `global_cache`).
- **Lazy collaborator:** `SonarrSyncTagsManager`, resolved/created on demand by `get_tag_monitor()`.
- **Brain modules:** none directly — value-judgements live in `machine_learning/`, invoked by the service managers built on top of this class, not here.
