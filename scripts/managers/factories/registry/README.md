# RegistryManager

- **File** — `scripts/managers/factories/registry/__init__.py`
- **One-liner** — The process-wide service registry: a single in-memory directory of every live manager instance (plus boolean flags, component health, and origin metadata) that the whole manager tree shares.

## What it does (for a senior Python engineer)

`RegistryManager` is a thin composition class. It does **not** add behavior of its own beyond plumbing; it inherits all of its public surface from six mixins via multiple inheritance:

```python
class RegistryManager(
    RegistryCore,       # the actual store + register/get/flag/find helpers
    RegistryTracer,     # call-stack origin tracing
    RegistryCLI,        # pretty-printed registry dump
    RegistryInjection,  # push shared deps down a manager subtree
    RegistryHealth,     # component_status (ok/failed) tracking
    RegistryConfigSync, # re-register on config change + propagate a config attr
):
```

It is the `registry` dependency that `BaseManager` injects into every manager (`base_manager.py` line 40: `self.registry = registry or RegistryManager()`), and the object every manager self-registers into (`base_manager.py` line 65: `self.registry.register("manager", self.name, self)`).

### Singleton model (important subtlety)

There are **two** singleton mechanisms here, and they do not fully overlap:

- `RegistryCore.__new__` is a classic thread-safe singleton (guarded by `_class_lock`); the *first* instance created of any class in the `RegistryCore` MRO becomes `RegistryCore._instance`, and that one instance owns the real `_registry` dict and an `RLock` (`_lock`). Because `RegistryManager` subclasses `RegistryCore`, calling `RegistryManager()` repeatedly returns the same object, so `_registry` is genuinely process-wide.
- The module also exports `get_registry()` which simply does `return RegistryManager()` — i.e. relies on the `__new__` singleton above. This is the documented "Singleton export."
- `RegistryManager.__init__` runs on every `RegistryManager()` call (Python always calls `__init__` even when `__new__` returns an existing instance). It is written defensively: `self._registry = getattr(self, "_registry", {})` preserves the already-populated dict rather than wiping it, and `self.registry = self` makes the object its own `registry` alias so the mixins (which were originally written to receive a `registry` arg) can reach the core store via `self.registry`/`self._registry`.

### Public methods (inherited)

From **RegistryCore** (the store):
- `register(category, name, obj, parent_name=None)` — store `obj` under `_registry[category][name]` as `{"instance", "origin", "parent_name"}`. Walks `inspect.stack()` to find the first non-utility frame and records it as `origin` (skips logger/timing/decorator/`registry/core`/`base_manager` frames). Also stamps `_registry_category`, `_registry_name`, `_registered_class`, `_registered_from`, and `parent_name` directly onto the object. Lock-guarded.
- `get(category, name)` — return the stored instance (unwraps the `{"instance": …}` dict).
- `set(category, name, value)` — store a raw value (not wrapped) and stamp class metadata. Lock-guarded.
- `remove(category, name)` — delete an entry. Lock-guarded.
- `get_all(category)` — return `{name: instance}` for a category (unwrapped).
- `get_all_verbose(category)` — return the full wrapped dict including `origin`/`parent_name`.
- `list_registered(category=None, include_origin=True)` — formatted `name → "ClassName @ origin"` map; one category or all.
- `find_by_attr(attr_name, attr_value)` — linear scan returning `[(category, name, obj), …]` whose attribute matches.
- Flag helpers: `set_flag(flag_name, value=True)`, `get_flag(flag_name)` (returns `None` if absent), `has_flag(flag_name)` (returns `False` if absent), `clear_flags(prefix=None)` (clears all, or only keys with `prefix`). These live under `_registry["flags"]`. **Note:** `RegistryManager` explicitly re-declares `set_flag`/`get_flag`/`has_flag`/`clear_flags` as forwarders to `RegistryCore.<name>(self, …)` — redundant given the MRO, but harmless.

From **RegistryHealth** (component status):
- `set_component_status(key, status: bool)` — record per-component ok/failed under `_registry["component_status"]`.
- `get_component_status(key)` — read one component's status.
- `get_all_failed_components()` — list every key whose status is `False`.

From **RegistryInjection**:
- `inject_dependencies_for_subtree(root_name, category="manager")` — recursively walk every manager whose `parent_name == root_name`, copy `logger`/`config`/`global_cache`/`validator` from the root onto the child, set the child's `registry`, call `_log_init_summary()` if present, then recurse.

From **RegistryConfigSync**:
- `auto_hot_swap_from_config(obj)` — re-register an object (using its `_registry_category` + `name`) so a config-driven swap takes effect; warns if the object lacks the required attributes.
- `load_config_and_propagate(key)` — read attribute `key` off the registered `ConfigManager` and push it onto every registered object that already has that attribute.

From **RegistryCLI**:
- `print_detailed_registry(category="manager")` — build a `PrettyTable` (`Manager | Class | Parent | Source | Anomaly`) and log it; flags any `source` path containing `pycharmprojects` as `❌ Suspicious file path`.
- Helpers: `_split_camel_case(name)`, `_is_expected_path(name, klass, origin_path)` (checks an origin path matches the service/CamelCase-derived module path for sonarr/radarr/trakt/tautulli).

From **RegistryTracer**:
- `trace_real_caller()` (staticmethod) — walk the stack skipping known decorator/util files (`timing.py`, `logger.py`, `decorators.py`, `registry.py`, `base_manager.py`) and return `(file, line, func, class)` of the real caller, climbing past a non-`*Manager` `__init__` to find the owning `*Manager`.

### FETCH / CACHE / APPLY

**None.** This is pure in-process infrastructure. It touches no external API, no `global_cache`, and no Parquet. `_registry` is an ordinary in-memory `dict` that lives only for the process lifetime.

### Config keys

Reads none directly. `RegistryConfigSync.load_config_and_propagate(key)` reads an *arbitrary attribute* (named by its `key` argument) off the registered `ConfigManager` instance and copies it to other registered objects — but the registry itself hardcodes no config key.

### dry_run

Not applicable — the registry never performs side-effecting APPLY work, so there is nothing to suppress under `dry_run`.

### Concurrency / threading

- `RegistryCore.__new__` uses a class-level `threading.Lock` to make singleton construction race-free.
- Mutations (`register`, `set`, `remove`, all flag helpers) take a per-instance `threading.RLock` (`_lock`). Read methods (`get`, `get_all`, `get_component_status`, …) are **not** lock-guarded, which is the usual CPython "dict reads are atomic enough" assumption.

## How it functions

Lifecycle:

1. The first `RegistryManager()` (or `get_registry()`) call triggers `RegistryCore.__new__`, which creates the singleton and initializes `_registry = {}` and `_lock = RLock()`.
2. `RegistryManager.__init__` runs, re-binding `self._registry` to the existing dict (or `{}` if somehow absent) and setting `self.registry = self`.
3. From then on, `BaseManager.__init__` calls `self.registry.register("manager", self.name, self)` for every manager constructed in the tree, so the registry fills up as `main.py` builds Sonarr/Radarr/Trakt/Tautulli and their submanagers.
4. `ComponentManagerMixin.load_components` sets `"<prefix>.<name>_initialized"` flags via `set_flag`, and managers/health checks record ok/failed via `set_component_status`.
5. `inject_dependencies_for_subtree` / `load_config_and_propagate` are invoked on demand to fan shared deps or a config value down the already-registered tree.

Notable internal helpers: the stack-walking origin logic in `register` and `trace_real_caller` both exist to answer "who actually created/registered this object," deliberately skipping decorator/logger/base-manager frames so the recorded origin points at real application code.

No decision is delegated to any `machine_learning` brain module — this class is plumbing, not policy.

**Known gap (documented, not invented):** `base_manager.py` line 68 calls `self.registry.print_tree_view(category="manager")`, but no `print_tree_view` method is defined anywhere in this `registry/` package. The CLI mixin only provides `print_detailed_registry`. The call site appears to be inside a guarded block, so this is a latent/dead reference rather than a live crash, but it is genuinely unresolved from these files.

## Criteria & examples

- **Singleton identity** — `a = RegistryManager(); b = get_registry()` ⇒ `a is b is True`, and any object `a.register(...)`-ed is visible via `b.get(...)`, because both share `RegistryCore._instance` and its `_registry` dict.
- **Origin tracing** — when `SonarrInstanceManager` is constructed and `register("manager", "SonarrInstanceManager", self)` runs, the stack walk skips frames in `factories/base_manager`, `utilities/logger`, `utilities/decorators`, and `registry/core`, so the recorded `origin` is the first real frame, e.g. `.../managers/services/sonarr/instance.py:42 in __init__()` — not the base-manager frame that actually issued the call.
- **Flag defaults** — after `load_components` does `set_flag("sonarr.tags_initialized", True)`, then `has_flag("sonarr.tags_initialized")` ⇒ `True`, while `has_flag("sonarr.missing")` ⇒ `False` (absent key defaults to `False`), and `get_flag("sonarr.missing")` ⇒ `None` (absent key defaults to `None`). `clear_flags(prefix="sonarr.")` removes only the `sonarr.*` flags and leaves `radarr.*` intact.
- **Failed-component scan** — if `set_component_status("trakt.history", False)` was recorded and everything else is `True`, then `get_all_failed_components()` ⇒ `["trakt.history"]`.
- **Anomaly flag** — in `print_detailed_registry`, an entry whose `source` is e.g. `C:/Users/dev/PyCharmProjects/old/sonarr.py` (contains `pycharmprojects`, case-insensitive) gets the `Anomaly` cell `❌ Suspicious file path`, surfacing a manager that was imported from a stale checkout.

## In plain English

Think of this as the staff directory and switchboard for the whole app. Every "department head" (manager) — the Sonarr team, the Radarr team, the Trakt team — signs in at the front desk the moment it's created, leaving its name, who its boss is, and which office it was hired from. The directory is a single shared binder (there's only ever one copy), so anyone can look up "who's running the Sonarr show right now?" and get the live person, not a stale memo.

It also keeps a little status board: green check or red X next to each team ("the Trakt history fetch failed"), a set of sticky-note flags ("Sonarr tags are set up: yes"), and a tool to hand every member of a department the same shared supplies (the logger, the config, the cache) so nobody is working with their own private copy. None of this is about *deciding* what to watch or delete — it's the org chart and intercom that lets all the other parts of Glidearr find and talk to each other.

## Interactions

- **Constructed/used by `BaseManager`** (`factories/base_manager.py`): injected as `self.registry`, used for self-registration, parent lookup (`registry.get("manager", parent_name)`), config hot-swap, and sibling lookups (e.g. `registry.get("manager", "SonarrSyncTagsManager")`).
- **Driven by `ComponentManagerMixin`** (`factories/mixins/component_manager.py`): sets the per-component `*_initialized` flags via `set_flag`.
- **Composed entirely of its six sibling helper classes** in this same directory — `RegistryCore` (`core.py`), `RegistryTracer` (`trace.py`), `RegistryCLI` (`cli.py`), `RegistryInjection` (`injection.py`), `RegistryHealth` (`health.py`), `RegistryConfigSync` (`config_sync.py`). These are intentionally **not** documented separately here because their class names do not end in `Manager`; they are summarized inline above as the source of `RegistryManager`'s public methods.
- **Brain modules:** none. The registry delegates no decision to `machine_learning/`.
- **External services:** none.
