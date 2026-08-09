# registry — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [factories](../README.md) › **registry**

**Package** — `scripts.managers.factories.registry`
**Status** — ✅ Implemented · 🟡 One dead reference (`print_tree_view`)
**Related** — [README.md](./README.md) · [`factories/DESIGN.md`](../DESIGN.md)

---

## 1. Problem statement

Roughly four hundred manager modules need to find each other at runtime, and the
obvious solutions all fail at this scale:

- **Import each other directly** → circular imports, and a compile-time graph
  that cannot express "the Sonarr tag monitor, whichever instance is live."
- **Pass handles down the constructor chain** → every manager's signature grows
  with the union of everything its descendants might need.
- **Module-level globals** → untestable, and multi-instance Radarr/Sonarr becomes
  impossible.

There is also a second, subtler need. When something goes wrong at this scale,
the first question is *"which object is actually live, and where was it created
from?"* — and with a stale mirror checkout in play (a real, recurring hazard in
this project's history), "which copy of the file did this come from?" is not a
rhetorical question.

The registry answers both: a single in-memory directory of every live manager,
**plus the origin frame it was registered from.**

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | One process-wide directory of live manager instances. |
| G2 | Lookup by name, without an import. |
| G3 | Record *where* each object was really created — skipping decorator/logger/base-class frames. |
| G4 | Boolean flags and component health in the same store, so run state is inspectable in one place. |
| G5 | Push shared dependencies down a subtree on demand. |
| G6 | Surface a stale-checkout import as an explicit anomaly. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Persistence | Process-lifetime only. Nothing here survives a restart by design. |
| N2 | Full DI container with scopes and lifecycles | The actual need is a name→instance map plus flags. |
| N3 | Type-safe lookup | `get()` returns `Any`. Callers know what they asked for. |
| N4 | Distributed/multi-process registry | Single process. |

---

## 3. Architecture

### 3.1 Component map

`RegistryManager` adds no behaviour of its own — it is pure composition over six
mixins:

```python
class RegistryManager(
    RegistryCore,        # core.py        — the store, register/get/flags
    RegistryTracer,      # trace.py       — call-stack origin tracing
    RegistryCLI,         # cli.py         — PrettyTable dump + anomaly flags
    RegistryInjection,   # injection.py   — push deps down a subtree
    RegistryHealth,      # health.py      — component ok/failed status
    RegistryConfigSync,  # config_sync.py — re-register on config change
): ...
```

Store shape:

```
_registry = {
    "manager":          {name: {"instance", "origin", "parent_name"}},
    "flags":            {flag_name: bool},
    "component_status": {key: bool},
    <other categories>: {...},
}
```

### 3.2 The two singleton mechanisms

This is the part worth understanding before touching anything here.

1. `RegistryCore.__new__` is a classic `_class_lock`-guarded singleton. The first
   instance of any class in the `RegistryCore` MRO becomes `RegistryCore._instance`
   and owns the real `_registry` dict and its `RLock`.
2. `get_registry()` is simply `return RegistryManager()` — relying on (1).

Because Python always calls `__init__` after `__new__` returns an instance of
`cls`, **`RegistryManager.__init__` runs on every construction.** It is therefore
written defensively:

```python
self._registry = getattr(self, "_registry", {})   # preserve, never wipe
self.registry  = self                             # self-alias for the mixins
```

A naive `self._registry = {}` would silently erase the entire directory on the
second `RegistryManager()` call anywhere in the process. That single line is
load-bearing.

The `self.registry = self` alias exists because the mixins were originally
written to receive a `registry` argument; aliasing lets them reach the core store
unchanged.

### 3.3 Origin tracing

`register()` walks `inspect.stack()` to find the first frame that is *not*
infrastructure — skipping `logger`, `timing`, `decorators`, `registry/core`, and
`base_manager`. The result is stamped on both the entry and the object itself:
`_registry_category`, `_registry_name`, `_registered_class`, `_registered_from`,
`parent_name`.

This matters because `BaseManager.__init__` is what actually issues the
`register()` call. Without frame-skipping, every single manager would report its
origin as `base_manager.py:65` — true, and useless.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Composition over six mixins | Each concern is independently readable and testable; `RegistryManager` stays a declaration | One 600-line class |
| D2 | `__init__` preserves `_registry` | Direct consequence of Python's `__new__`/`__init__` contract (§3.2) | Guard flag |
| D3 | `self.registry = self` self-alias | Lets mixins written for injection work unchanged under inheritance | Rewrite all six mixins |
| D4 | Frame-skipping origin trace | G3 — the naive answer is always `base_manager.py` | `__file__` of the class |
| D5 | Flags and health in the same store | One place to inspect run state; `clear_flags(prefix=...)` gives cheap namespacing | Separate objects |
| D6 | `has_flag` → `False`, `get_flag` → `None` when absent | Distinguishes "explicitly false" from "never set" — the two mean different things during a partial run | Both return `False` |
| D7 | Writes locked, reads not | Mutations are rare and correctness-critical; reads are hot and dict reads are atomic under CPython | Lock everything |
| D8 | `pycharmprojects` in an origin path ⇒ `❌ Suspicious file path` | G6 — a manager imported from a stale checkout is a real, previously-experienced failure that is otherwise invisible | No anomaly detection |

**On D8:** this project has a documented history of a stale mirror directory
producing incorrect diagnoses. The anomaly flag exists to make "you are running
code from the wrong checkout" visible in the registry dump rather than inferred
three hours later.

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | Exactly one `_registry` dict per process. |
| I2 | `RegistryManager()` and `get_registry()` return the same object. |
| I3 | Constructing `RegistryManager()` never clears existing entries. |
| I4 | Every `BaseManager` self-registers under category `"manager"`. |
| I5 | `origin` never points at an infrastructure frame. |
| I6 | `has_flag(absent)` → `False`; `get_flag(absent)` → `None`. |
| I7 | `clear_flags(prefix="sonarr.")` leaves `radarr.*` untouched. |
| I8 | The registry performs no FETCH, no CACHE, no APPLY. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius |
|---|---|---|---|
| Name collision (two classes, same name) | None | Second registration **overwrites** the first, silently | Wrong instance returned |
| Parent not yet registered | `get` → `None` | `BaseManager._resolve_deferred_parent()` retries | None if resolved |
| Parent never registers | Retry fails | Manager keeps its own deps — silent divergence | Subtle |
| `print_tree_view` called | — | **No such method exists** in this package. `base_manager.py:68` calls it inside a guarded block → latent dead reference | None today |
| Concurrent read during write | None | `RLock` covers writers only; readers may see a partially-updated dict | Theoretical under CPython |
| `auto_hot_swap_from_config` on an object lacking `_registry_category` | Attribute check | Warns, skips | One object |
| Circular parent chain | None | `inject_dependencies_for_subtree` would recurse indefinitely | Stack overflow |
| **Anomaly column fires for every row** | 🔴 **Confirmed defect** — see §6.1 | Every manager reports `❌ Suspicious file path` | Column is pure noise |

**Rows 1 and 7 are unguarded.** Name collision is plausible (e.g. two `CacheManager`
classes in different services); circular parent chains are unlikely but
catastrophic. Both are cheap to guard — §9 P1, P2.

### 6.1 🔴 Confirmed defect: the anomaly heuristic is inverted

[`cli.py`](./cli.py) flags a registry entry as suspicious like this:

```python
if isinstance(source, str) and "pycharmprojects" in source.lower():
    anomaly = "❌ Suspicious file path"
```

The canonical repo is now `C:\Users\rober\PycharmProjects\glidearr`. Every
manager registered during a normal run therefore has `pycharmprojects` in its
origin path and is flagged.

The polarity is backwards relative to today's layout. The rule was written when
the working repo lived elsewhere and the PyCharm copy was the stale one; that
relationship has since reversed. As written, the `Anomaly` column marks the
**correct** checkout as suspicious and would stay blank for an import from the
actual stale mirror — the precise inverse of its purpose (D8, G6).

**Second finding in the same file:** `_is_expected_path()` is fully implemented
(deriving an expected module path from the CamelCase class name for
sonarr/radarr/trakt/tautulli) but is **never called** by
`print_detailed_registry` or anything else in the package. It is dead code — and
it is a markedly better anomaly signal than the substring check, because it
validates that a class lives where its name says it should.

Fix direction: drop the substring rule, wire `_is_expected_path()` into the
anomaly column, and flag entries whose origin does **not** match the expected
service path. Tracked as §9 P11.

---

## 7. Configuration surface

Reads no config key directly.

`RegistryConfigSync.load_config_and_propagate(key)` reads an **arbitrary
attribute named by its argument** off the registered `ConfigManager` and copies it
onto every registered object that already has an attribute of that name. The
registry hardcodes nothing.

---

## 8. Implemented capabilities

- ✅ Thread-safe singleton with preserved store across re-construction
- ✅ `register` / `get` / `set` / `remove` / `get_all` / `get_all_verbose`
- ✅ `list_registered` with origin formatting
- ✅ `find_by_attr` linear scan
- ✅ Flags: `set_flag` / `get_flag` / `has_flag` / `clear_flags(prefix)`
- ✅ Component health: `set_component_status` / `get_component_status` / `get_all_failed_components`
- ✅ Origin tracing with infrastructure-frame skipping
- ✅ `trace_real_caller()` climbing to the owning `*Manager`
- ✅ `inject_dependencies_for_subtree` recursive dependency push
- ✅ `auto_hot_swap_from_config` re-registration
- ✅ `print_detailed_registry` PrettyTable with stale-checkout anomaly flag

## 9. Planned additions

| # | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| P1 | **Warn on name collision** in `register()` when replacing a different class | Closes §6 row 1, which is currently silent and returns the wrong object | S | — |
| P2 | **Cycle detection** in `inject_dependencies_for_subtree` (visited set) | Prevents stack overflow on a malformed parent chain | S | — |
| P3 | **Implement or remove `print_tree_view`** | Deletes the dead reference; a real tree view is genuinely useful for debugging parent links | S | — |
| P4 | **Snapshot the registry to disk** at run end | Post-mortem "what was actually live?" | S | P3 |
| P5 | **Registry view in the web UI** | Live tree, flags, failed components in the browser | M | [`web/`](../web/DESIGN.md) |
| P6 | **Typed lookup** — `get(category, name, expect=SomeClass)` raising on mismatch | Catches wiring errors at the call site rather than three frames later | S | — |
| P7 | **Read lock or immutable snapshot reads** | Closes §6 row 5 properly rather than relying on CPython atomicity | M | — |
| P8 | **Registration timestamps** | Enables "what order did the tree actually build in?" — useful for the ordering bugs this layer keeps producing | S | — |
| P9 | **Flag namespace validation** — reject flags without a known prefix | Prevents typo'd flags reading as permanently `False` | S | — |
| P10 | **Deregistration on manager teardown** | Currently entries live until process exit; matters if the web layer hosts multiple runs | M | [`web/`](../web/DESIGN.md) |
| P11 | **🔴 Fix the inverted anomaly heuristic** — remove the `pycharmprojects` substring rule and wire the already-written `_is_expected_path()` into the anomaly column | Restores D8/G6. Currently the column flags every row and would stay silent on a genuine stale-mirror import | S | §6.1 |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Should a name collision be fatal, or a warning? Fatal is correct but may break existing accidental reuse. | P1 |
| Q2 | Is `find_by_attr`'s linear scan ever hot enough to need an index? | — |
| Q3 | Should the six mixins be documented individually? They are currently summarised in [README.md](./README.md) because their names do not end in `Manager`. | — |
| Q4 | ~~Does the anomaly rule produce false positives now the canonical repo is under `PycharmProjects`?~~ **Answered: yes, confirmed — see §6.1.** | P11 |
| Q5 | Should the canonical repo root be read from config rather than hardcoded, so the anomaly rule survives a future move? | P11 |

## 11. Related designs

- [`factories/DESIGN.md`](../DESIGN.md) §3.2 — dependency inheritance
- [`base_manager.md`](../base_manager.md) — the primary consumer
- [`mixins/component_manager.md`](../mixins/component_manager.md) — sets the `*_initialized` flags
- [`web/DESIGN.md`](../web/DESIGN.md) — registry health view
