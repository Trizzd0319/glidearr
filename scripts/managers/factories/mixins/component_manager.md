# ComponentManagerMixin

- **File** — `scripts/managers/factories/mixins/component_manager.py`
- **One-liner** — A mixin that lets any manager dynamically instantiate, dependency-inject, register, and tabulate a set of submanager classes, then print one summary line about which loaded.

## What it does (for a senior Python engineer)

`ComponentManagerMixin` is a pure behavioral mixin (no `__init__`, no state of its own) that a host manager class inherits *alongside* `BaseManager`. The canonical host is `Main` (`scripts/main.py`, `class Main(BaseManager, ComponentManagerMixin)`), but every service manager that loads submanagers (Sonarr, Radarr, Trakt, Tautulli, etc.) mixes it in. It does not sit anywhere in the manager tree itself — it is a capability bundle, not a registered manager — so it performs none of FETCH / CACHE / APPLY directly. It touches no external API endpoints, reads no config keys, and reads/writes no `global_cache` or Parquet keys. Its only side effects are: setting attributes on `self`, setting registry flags, and logging.

It assumes the host already carries the shared dependencies that `BaseManager.__init__` injects (`logger`, `config`, `global_cache`, `validator`, `registry`, and optionally `metrics`, `instance_manager`, and an `api`-like attribute). All four public methods read those off `self` via `getattr`.

Key public methods:

- **`load_components(component_map, registry_prefix, api_kwarg_name="api", **kwargs)`** — The core method. `component_map` is a `{attribute_name: SubmanagerClass}` dict. For each entry it constructs `cls(**injected, **cleaned_kwargs)` and attaches the instance to the host as `self.<attribute_name>`. The `injected` dict is the shared dependency set assembled from the host:
  - the API kwarg, passed under the *name* given by `api_kwarg_name` (default `"api"`; Sonarr/Radarr pass their service-specific name, e.g. `sonarr_api`). Its value is `getattr(self, api_kwarg_name)` falling back to `getattr(self, "api")`.
  - `manager=self` (so each submanager knows its parent),
  - `instance_manager`, `logger`, `config`, `global_cache`, `validator`, `registry`, `metrics` — each pulled off `self` via `getattr(..., None)`.
  - Any extra `**kwargs` the caller passes are forwarded as `cleaned_kwargs`, *minus* a guard set (`excluded_keys`) that strips out the injected dependency names so a caller can't accidentally double-pass them and trigger a duplicate-keyword `TypeError`.

  Per component it sets a registry flag `"<registry_prefix>.<name>_initialized"` to `True` on success or `False` on failure (failures are caught per-component, logged via `logger.log_error`, and do not abort the loop). It records `"✅"`/`"❌"` in `self.load_summary` (a dict it creates), emits ONE summary line `[<HostClassName>] <n_ok>/<n_total>: name1✅  name2✅  name3❌`, and returns a `{name: instance}` dict of the components that actually attached.

- **`log_filtered_component_summary(service_name, component_label, critical_components, noncritical_components, all_critical_loaded)`** — Silently appends a row to a lazily-created `self.logger._component_summary_rows` list for an end-of-run table. Emits no inline log. If `all_critical_loaded` is `False`, it computes the failed names as the union of critical + noncritical components whose `self.load_summary` value does not start with `"✅"`; otherwise the failures column is empty.

- **`log_final_run_summary()`** — At end of run, renders `self.logger._component_summary_rows` as a `tabulate` table (headers `Service / Manager / Status / Failures`, `simple` format), logs it, then deletes the attribute. No-ops if no rows were ever accumulated.

- **`register(parent_name=None, **kwargs)`** — Registers `self` in the registry under category `"manager"` keyed by `self.name`, resolving the parent: explicit `parent_name`, else `self.manager.__class__.__name__`, else `self.parent_name`. Wrapped in `@LoggerManager().log_function_entry` and `@timeit("register")`. Registry failures are caught and downgraded to a warning.

Concurrency/threading: no locks or threads here. (Singleton behavior and the auto-link/registry machinery live in `BaseManager`, not this mixin.)

## How it functions

There is no lifecycle of its own — the mixin is invoked imperatively by a host manager, typically during that host's own `prepare()`/init after `BaseManager.__init__` has populated the shared deps. The usual flow:

1. Host builds a `component_map` dict mapping desired attribute names to submanager classes.
2. Host calls `self.load_components(component_map, registry_prefix="sonarr", api_kwarg_name="sonarr_api")` (names illustrative).
3. `load_components` injects shared deps, instantiates each submanager (which themselves are typically `BaseManager` subclasses that auto-link back to this host via `manager=self`), attaches them, flags the registry, and logs the one-line summary.
4. Later, `log_filtered_component_summary` may be called to stage a row, and `log_final_run_summary` prints the consolidated end-of-run table.

Internal notable detail: the `excluded_keys`/`cleaned_kwargs` split is the guard that makes the injection idempotent against caller-supplied duplicates. The `api_value` fallback (`api_kwarg_name` then `"api"`) is what lets service-specific API attribute names coexist with the generic default.

This mixin delegates no decision to any `machine_learning` brain module — it is pure plumbing for wiring submanagers together.

## Criteria & examples

- **Per-component isolation:** a failure to construct one submanager does not stop the others. Concrete example: with `component_map = {"cache": CacheMgr, "router": RouterMgr, "anomaly": AnomalyMgr}`, if `RouterMgr(**injected)` raises, the loop catches it, logs `[SonarrManager] ❌ router (RouterMgr): <error>`, sets registry flag `sonarr.router_initialized = False`, records `router → ❌`, and still constructs `anomaly`. The summary line becomes `[SonarrManager] 2/3: cache✅  router❌  anomaly✅` and the returned dict contains only `cache` and `anomaly`.

- **Failure detection in the filtered summary:** `log_filtered_component_summary` treats a component as failed unless its `load_summary` value *starts with* `"✅"`. So a `load_summary["router"] == "❌"` (or missing → `""`) counts as a failure and lands in the table's Failures column; only an exact-prefix `"✅"` clears it.

- **Parent resolution order in `register`:** given a submanager with `self.manager` set to a `SonarrManager` instance and `self.parent_name = "Sonarr"`, calling `register()` with no argument resolves the parent to `"SonarrManager"` (the class name wins because `self.manager` is checked before `self.parent_name`).

## In plain English

Think of a manager as a film director and its submanagers as the crew — camera, sound, lighting. This mixin is the assistant director who, on day one, hands every crew member the same shared kit (the call sheet, the radios, the schedule) so nobody works off a different copy, checks each person in, and then reads out a quick roll-call: "Camera ✅, Sound ✅, Lighting ❌." If the lighting tech never shows up, the shoot doesn't collapse — it's noted on the board, and everyone else still gets to work. At the end of the day it posts one tidy status sheet so you can see at a glance which departments were fully staffed and which had a no-show.

## Interactions

- **Hosts (the classes that mix it in):** `Main` and the top-level service managers (Sonarr, Radarr, Trakt, Tautulli, ...). The mixin operates entirely through attributes those hosts inherit from `BaseManager`.
- **Submanagers:** instantiates whatever classes the host passes in `component_map`, injecting `manager=self` so each links back to its parent (the reciprocal of `BaseManager`'s auto-link).
- **Registry (`RegistryManager`):** sets `"<prefix>.<name>_initialized"` flags and registers managers under category `"manager"`.
- **Logger (`LoggerManager`):** all summary output, the lazily-attached `_component_summary_rows`, and (via `log_function_entry`) `register`'s entry trace.
- **Brain modules:** none — this mixin makes no value judgements and delegates nothing to `machine_learning/`.
