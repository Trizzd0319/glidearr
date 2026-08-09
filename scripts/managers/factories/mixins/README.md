# mixins

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [factories](../README.md) › **mixins**

**Package** — `scripts.managers.factories.mixins`
**Run position** — Invoked imperatively by a host manager, typically during its `__init__` or `prepare()`, after `BaseManager.__init__` has populated the shared dependencies.
**One-liner** — Behavioural mixins that give any manager declarative subcomponent loading, ordered initialisation, and queue cancellation — without adding a node to the manager tree.

---

## Purpose

A `BaseManager` gives you identity, dependencies and a parent link. It does not
give you **children**. Nearly every service manager owns a set of submanagers
(Sonarr owns cache, sync, quality, repair, monitoring, storage…) and each one
needs the same shared dependency set injected, the same registry flag set, and
the same failure isolation.

Writing that loop by hand in every service manager would be ~40 lines duplicated
a dozen times, each copy free to drift. `ComponentManagerMixin` is that loop,
written once.

These are **capability bundles, not managers.** They hold no state, define no
`__init__`, and never register themselves in the registry.

---

## Script inventory

| Script | Doc | Role | Status |
|---|---|---|---|
| [`component_manager.py`](./component_manager.py) | [`component_manager.md`](./component_manager.md) | `ComponentManagerMixin` — declarative subcomponent construction, DI, registry flagging, one-line summary | ✅ Implemented |
| [`mixins.py`](./mixins.py) | [`mixins.md`](./mixins.md) | Shared mixin helpers | ✅ Implemented |
| [`ordered_components.py`](./ordered_components.py) | — | Deterministic component ordering where init order matters | ✅ Implemented |
| [`queue_cancel.py`](./queue_cancel.py) | — | Cooperative cancellation for queued work | ✅ Implemented |
| [`__init__.py`](./__init__.py) | — | Package marker | ✅ Implemented |

## Test coverage

| Test | Covers |
|---|---|
| [`test_ordered_components.py`](./test_ordered_components.py) | Ordering guarantees in [`ordered_components.py`](./ordered_components.py) |

---

## Entry points

| Symbol | Role |
|---|---|
| `ComponentManagerMixin.load_components(component_map, registry_prefix, api_kwarg_name="api", **kwargs)` | The core method — construct, inject, attach, flag, summarise |
| `ComponentManagerMixin.log_filtered_component_summary(...)` | Stage a row for the end-of-run table |
| `ComponentManagerMixin.log_final_run_summary()` | Render the staged rows as a `tabulate` table, then clear |
| `ComponentManagerMixin.register(parent_name=None, **kwargs)` | Self-register under category `"manager"` |

### `load_components` in one line

```python
self.load_components(
    {"cache": SonarrCacheManager, "sync": SonarrSyncManager},
    registry_prefix="sonarr",
    api_kwarg_name="sonarr_api",
)
```

Injected into every component automatically: the API handle (under
`api_kwarg_name`), `manager=self`, `instance_manager`, `logger`, `config`,
`global_cache`, `validator`, `registry`, `metrics`.

Emits exactly one line:

```
[SonarrManager] 2/3: cache✅  sync✅  router❌
```

---

## Data in / data out

| Direction | Source/Sink | Payload |
|---|---|---|
| IN | `self` (host manager) | Shared dependencies, read via `getattr` |
| IN | `component_map` | `{attribute_name: SubmanagerClass}` |
| OUT | `self.<attribute_name>` | Constructed submanager instances |
| OUT | Registry flags | `"<prefix>.<name>_initialized"` → `True` / `False` |
| OUT | `self.load_summary` | `{name: "✅" \| "❌"}` |
| OUT | Log | One summary line per host |

**No FETCH. No CACHE. No APPLY.** No config keys read. No brain delegation.

---

## Navigation

- **Up:** [`factories/`](../README.md)
- **Design:** [`DESIGN.md`](./DESIGN.md)
- **Related:** [`base_manager.md`](../base_manager.md) · [`registry/README.md`](../registry/README.md)
