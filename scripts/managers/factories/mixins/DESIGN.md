# mixins — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [factories](../README.md) › **mixins**

**Package** — `scripts.managers.factories.mixins`
**Status** — ✅ Implemented · 🔴 One known defect (false `❌` in prepare summaries)
**Related** — [README.md](./README.md) · [`component_manager.md`](./component_manager.md) · [`mixins.md`](./mixins.md)

---

## 1. Problem statement

Every service manager owns submanagers, and each one needs identical treatment:

- constructed with the same shared dependency set,
- attached to the host under a known attribute name,
- flagged in the registry so other code can check readiness,
- **isolated** — one failing submanager must not prevent the others loading,
- summarised in exactly one log line, per the project-wide logging standard.

Hand-writing that in a dozen service managers guarantees drift: one copy forgets
`metrics`, another lets an exception escape and aborts the rest, a third logs
per-component and floods the output.

There is a second, subtler requirement. Callers pass extra `**kwargs` through to
their components. If a caller passes `logger=` explicitly and the mixin also
injects `logger=`, Python raises `TypeError: got multiple values for keyword
argument`. The wiring must be idempotent against caller-supplied duplicates.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | One place defines the shared dependency set pushed into submanagers. |
| G2 | Per-component failure isolation. |
| G3 | Exactly one summary line per host manager. |
| G4 | Caller kwargs can never collide with injected dependencies. |
| G5 | Service-specific API attribute names (`sonarr_api`, `radarr_api`) coexist with a generic default. |
| G6 | Zero state — a mixin adds capability, never a tree node. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Dependency resolution / topological ordering | [`ordered_components.py`](./ordered_components.py) handles explicit ordering where needed; general resolution is over-engineering here. |
| N2 | Lazy component construction | Components are cheap; eager keeps failures early and visible. |
| N3 | Being a registered manager | G6 — it is a capability bundle. |
| N4 | Thread safety | Loading happens on one thread during construction. |

---

## 3. Architecture

### 3.1 Component map

```
host manager  (class SonarrManager(BaseManager, ComponentManagerMixin))
   │
   │  load_components({"cache": CacheMgr, "sync": SyncMgr}, "sonarr", "sonarr_api")
   ▼
ComponentManagerMixin
   │
   ├─ assemble `injected` from self via getattr:
   │     <api_kwarg_name> → getattr(self, api_kwarg_name) or getattr(self, "api")
   │     manager=self, instance_manager, logger, config,
   │     global_cache, validator, registry, metrics
   │
   ├─ cleaned_kwargs = caller kwargs MINUS excluded_keys      ← G4
   │
   ├─ for name, cls in component_map:
   │       try:  setattr(self, name, cls(**injected, **cleaned_kwargs))
   │             registry.set_flag(f"{prefix}.{name}_initialized", True)
   │             load_summary[name] = "✅"
   │       except: log_error; flag False; load_summary[name] = "❌"   ← G2
   │
   └─ log ONE line: [Host] n_ok/n_total: a✅  b✅  c❌               ← G3
      return {name: instance} for those that attached
```

### 3.2 Control flow — the reciprocal link

`load_components` passes `manager=self` into every component. Combined with
`BaseManager`'s parent auto-linking, this closes the loop in both directions:

- **Downward**, the host names its children explicitly in `component_map`.
- **Upward**, each child receives `manager=self` and separately resolves
  `parent_name` through the registry.

A child therefore ends up linked to its parent by two independent mechanisms.
That redundancy is why a mis-inferred `parent_name` (see
[`factories/DESIGN.md`](../DESIGN.md) §6) is usually survivable — the
`manager=self` link still holds.

### 3.3 Data contracts

| Contract | Shape |
|---|---|
| `component_map` | `{attribute_name: SubmanagerClass}` |
| `load_summary` | `{name: "✅" \| "❌"}` — created by the mixin on the host |
| Registry flag | `"<registry_prefix>.<name>_initialized"` → `bool` |
| Return | `{name: instance}` for components that actually attached |
| `_component_summary_rows` | Lazily attached to the **logger**, not the host |

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Mixin, not a base class | Hosts already inherit `BaseManager`; a second base would force MRO gymnastics | Base class |
| D2 | `getattr(self, …, None)` for every dependency | Hosts vary — not all have `metrics` or `instance_manager`. Missing means `None`, not `AttributeError` | Required attributes |
| D3 | `excluded_keys` / `cleaned_kwargs` split | G4 — makes injection idempotent against caller duplicates, which would otherwise be a `TypeError` at construction | Let it raise |
| D4 | `api_kwarg_name` with fallback to `"api"` | G5 — Sonarr passes `sonarr_api`, Radarr `radarr_api`, generic hosts just `api` | Single fixed name |
| D5 | Per-component try/except | G2. One failing submanager degrades that capability, not the whole service | Fail fast |
| D6 | One summary line, details at error level | G3, matching the project-wide logging standard | Per-component info logs |
| D7 | Failure detection is `not value.startswith("✅")` | Treats missing and empty as failure — fails safe | Equality on `"❌"` |
| D8 | `_component_summary_rows` lives on the **logger** | Survives across hosts so the end-of-run table can span every service | Per-host storage |
| D9 | `register()` parent order: explicit → `self.manager.__class__.__name__` → `self.parent_name` | The live object beats the inferred name — the concrete link is more trustworthy | Prefer `parent_name` |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | A component failure never aborts the loop. |
| I2 | Exactly one summary line per `load_components` call. |
| I3 | Caller kwargs never collide with injected dependencies. |
| I4 | Every component receives `manager=self`. |
| I5 | Every component gets a registry flag, `True` or `False`. |
| I6 | The mixin holds no state of its own. |
| I7 | No FETCH, no CACHE, no APPLY, no config reads, no brain delegation. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius |
|---|---|---|---|
| Component constructor raises | try/except | Logged, flag `False`, summary `❌`, loop continues | That capability |
| Host lacks an expected attribute | `getattr(..., None)` | Component receives `None` — may fail later, further from the cause | Deferred, harder to diagnose |
| Component ignores an injected kwarg | `TypeError` on construction | Caught by D5 → shows as `❌` | Looks like a runtime failure, is actually a signature mismatch |
| Caller passes a duplicate kwarg | `excluded_keys` strips it | Injected value wins silently | Caller's value ignored without warning |
| **Component initialised before `prepare()`** | **None** | `load_summary` never populated → **false `❌`** for e.g. `instance_manager`, `radarr_cache` | 🔴 Misleading run output |
| `log_final_run_summary` with no rows | Attribute check | No-op | None |

### 6.1 🔴 Known defect: false negatives in the prepare summary

Components constructed **outside** `load_components` — directly in a host's
`__init__`, before `prepare()` runs — never get a `load_summary` entry. The
summary line then reports them as `❌` even though they are present and working.

Observed instances: `instance_manager❌`, `radarr_cache❌`.

This is purely a reporting defect: the components function correctly. But it
trains the operator to ignore `❌` in the one line that is supposed to be the
authoritative readiness signal, which defeats the purpose of the logging standard
it belongs to.

Fix direction: have directly-constructed components register their own
`load_summary` entry, or have `prepare()` reconcile `load_summary` against
attributes that are already non-`None` before emitting the line. Tracked as §9 P1.

---

## 7. Configuration surface

None. Reads no config key.

Behaviour is controlled entirely by call-site arguments: `component_map`,
`registry_prefix`, `api_kwarg_name`, and forwarded `**kwargs`.

---

## 8. Implemented capabilities

- ✅ Declarative `{attr: Class}` component construction
- ✅ Shared dependency injection assembled from the host
- ✅ Service-specific API kwarg naming with generic fallback
- ✅ `excluded_keys` guard against duplicate-kwarg `TypeError`
- ✅ Per-component failure isolation
- ✅ Registry flag per component
- ✅ `load_summary` tracking
- ✅ One-line host summary
- ✅ End-of-run consolidated `tabulate` table across services
- ✅ `register()` with three-tier parent resolution
- ✅ Ordered component initialisation ([`ordered_components.py`](./ordered_components.py), tested)
- ✅ Cooperative queue cancellation ([`queue_cancel.py`](./queue_cancel.py))

## 9. Planned additions

| # | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| P1 | **🔴 Populate `load_summary` for pre-`prepare()` components** | Removes the `instance_manager❌` / `radarr_cache❌` false negatives (§6.1) | S | — |
| P2 | **Warn on silently-dropped caller kwargs** | §6 row 4 discards a caller's value with no signal | S | — |
| P3 | **Distinguish "signature mismatch" from "runtime failure"** in the error line | §6 row 3 currently reports a `TypeError` identically to a real failure | S | — |
| P4 | **Declare required vs optional dependencies** per component, failing fast on a missing required one | Moves §6 row 2 from deferred to immediate | M | — |
| P5 | **Component load timing** in the summary | Feeds the performance audit; currently unmeasured | S | `timing.py` |
| P6 | **Dependency-ordered loading** via declared `depends_on`, generalising [`ordered_components.py`](./ordered_components.py) | Removes hand-maintained ordering | M | N1 revisited |
| P7 | **Lazy component construction** for expensive, rarely-used components | Faster startup | M | P4 |
| P8 | **Component health re-check** after load, not just construction success | "Constructed" ≠ "usable" | M | `registry/health.py` |
| P9 | **Typed `component_map`** (`Protocol` conformance check) | Catches signature mismatch statically rather than at construction | M | P3 |
| P10 | **Component tree in the web UI**, rendering `load_summary` live | Makes readiness visible without log parsing | M | [`web/`](../web/DESIGN.md) |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Should a *critical* component's failure be fatal, given `critical_keys` already exists on `BaseManager`? Currently every failure is equally survivable. | P4 |
| Q2 | Should injected values or caller kwargs win on collision? Injection currently wins silently. | P2 |
| Q3 | Is the end-of-run table still used, given the per-host one-liners? | — |
| Q4 | Should `load_summary` distinguish "not attempted" from "failed"? That distinction is exactly what §6.1 needs. | P1 |

**Q4 is the cleanest framing of the §6.1 fix:** the real problem is that
`load_summary` conflates *absent* with *failed*. A three-state value
(`✅` / `❌` / `—` not-attempted) would make the false negatives self-evidently
wrong rather than plausible.

## 11. Related designs

- [`component_manager.md`](./component_manager.md) — the mixin in detail
- [`mixins.md`](./mixins.md)
- [`base_manager.md`](../base_manager.md) — `prepare()` and `critical_keys`
- [`registry/DESIGN.md`](../registry/DESIGN.md) — flags and component health
- [`factories/DESIGN.md`](../DESIGN.md) §6
