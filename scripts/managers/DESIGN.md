# managers — Design

> Breadcrumb: [glidearr](../..) › [scripts](../README.md) › **managers**

**Package** — `scripts.managers`
**Status** — ✅ Implemented (layering) · 🟡 Partial (enforcement is one-directional)
**Related** — [README.md](./README.md) · [`scripts/DESIGN.md`](../DESIGN.md)

---

## 1. Problem statement

Glidearr integrates six external APIs and makes destructive decisions
(deletions, downgrades, re-grabs) from their combined signal. Two structural
failure modes destroy such a system over time:

1. **Scoring drift.** If acquisition ranks candidates one way and the deletion
   planner ranks them another, the system fights itself: it grabs a title on
   Monday and deletes it on Tuesday. This is not hypothetical — it is the default
   outcome when scoring logic is written inline at each call site.

2. **Untestability.** If decision logic is entangled with I/O, you cannot replay
   last month's library state to ask "would the new weights have made a better
   call?" Every scoring change becomes a live experiment on a real library.

The manager tree exists to make both structurally impossible rather than merely
discouraged.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | **One model.** Every ranking decision resolves to the same 100-point score. |
| G2 | **Replayability.** Any decision must be reproducible offline from cached inputs. |
| G3 | **Uniform lifecycle.** Every manager exposes the same `prepare()` / `run()` contract. |
| G4 | **Shared dependencies without plumbing.** One logger/config/cache/validator process-wide, without threading them through 400 constructors. |
| G5 | **Isolated failure.** A dead service degrades scope, not the run. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Runtime plugin loading | The manager set is known at build time. |
| N2 | Async/await throughout | The workload is I/O-bound but coarse-grained; threads at chosen seams are sufficient and far simpler. |
| N3 | Full DI container | The registry covers the actual need without the ceremony. |

---

## 3. Architecture

### 3.1 Component map

```
                       ┌──────────────┐
                       │  BaseManager │  singleton + DI + registry
                       └──────┬───────┘    + parent auto-link
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
   BaseInstance-        ComponentManager-      service managers
   Manager              Mixin                  (Radarr, Sonarr, …)
   (multi-instance      (declarative
    Radarr/Sonarr)       subcomponent load)
```

Every manager is a `BaseManager` subclass. It gets three things for free:

1. **Singleton identity**, keyed `(cls, singleton_key)`.
2. **Injected dependencies** — logger, config, `global_cache`, validator, registry.
3. **Parent auto-linking** — `parent_name` is either passed explicitly or inferred
   from the module's filesystem path, then resolved through the registry so the
   child inherits the parent's dependency set.

### 3.2 Control flow

Two phases, uniform across every manager:

| Phase | Method | Contract |
|---|---|---|
| Prepare | `prepare()` | Materialise `critical_keys` subcomponents. Emit exactly one summary line. No external I/O beyond warming. |
| Run | `run()` | Do the work. Base implementation is a no-op, so a manager that has nothing to do costs nothing. |

`Main` drives both phases explicitly rather than recursing, so ordering stays
visible in one file. See [`main.md`](../main.md).

### 3.3 Data contracts

The service↔brain boundary is the only contract that matters architecturally.
It is deliberately narrow:

```
service                             brain
───────                             ─────
gather rows ──► feature row dict ──► score / plan
                                          │
apply plan  ◄── Plan dataclass  ◄─────────┘
```

Shapes live in
[`machine_learning/contracts/`](./machine_learning/contracts/README.md):
`context.py`, `feature_rows.py`, `plans.py`. Plain data on both sides — no
manager handles, no cache handles, no clients.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Registry-based parent auto-linking | G4 without constructor plumbing at this depth | Explicit DI — unmanageable at ~400 modules |
| D2 | `_infer_parent_from_path()` heuristic | A manager under `services/sonarr/sync/` is *obviously* a child of `SonarrSync`; making that automatic removes a whole class of wiring bugs | Mandatory explicit `parent_name` |
| D3 | Singletons keyed `(cls, singleton_key)` | One Radarr client, one cache, process-wide | Per-call construction |
| D4 | `__init__` re-runs on singleton reuse | Python semantics; every `__init__` is therefore written to be **idempotent** | Guard flag — hides real re-init bugs |
| D5 | Brain purity enforced by hook, service purity by review | Import-boundary violations are AST-detectable; "this service is secretly scoring" is not | Enforce both mechanically |
| D6 | `run()` defaults to a logged no-op | Config-gated managers cost nothing when disabled | Abstract method — forces empty overrides everywhere |
| D7 | Deferred parent resolution (`_resolve_deferred_parent`) | Construction order cannot always precede registration | Strict topological construction order |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | Every manager subclasses `BaseManager` and self-registers under category `"manager"`. |
| I2 | `machine_learning/` imports no HTTP client, no `services.*`, no `*_api`. **Hook-enforced.** |
| I3 | `services/` contains no scoring, ranking or threshold logic. **Review-enforced.** |
| I4 | Every `__init__` is idempotent (D4). |
| I5 | `prepare()` emits exactly one summary line: `[Name] ✅ N/N: comp✅ comp✅`. |
| I6 | Only `orchestration/` and `Main` coordinate across two services. |
| I7 | Submodules never reach sideways into another service's managers. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius |
|---|---|---|---|
| Parent not yet registered | `registry.get` returns `None` | `_resolve_deferred_parent()` retries later; logs `🔗 Deferred linking` | None if resolved |
| Parent never registers | Deferred retry also fails | Manager keeps its own default logger/config — **silently diverges** from the shared set | Subtle; config changes won't reach it |
| `_infer_parent_from_path` mis-infers | None — no validation | Wrong or absent parent link | Silent dependency divergence |
| Component missing at `prepare()` | `load_summary` records `❌` | Summary line shows `❌`; manager continues degraded | Manager-local |
| Component initialised **before** `prepare()` | `load_summary` never populated | **False `❌`** in the summary — e.g. `instance_manager❌`, `radarr_cache❌` | Cosmetic but actively misleading |
| Two managers, same class, different `singleton_key` | By design | Separate instances — correct for multi-instance Radarr/Sonarr | Intended |

**Rows 2, 3 and 5 are the live pain points.** Rows 2–3 are silent-divergence
risks with no detection at all. Row 5 is the known false-negative in the prepare
summary and is tracked as §9 P2.

---

## 7. Configuration surface

`managers/` itself reads no config. Each layer documents its own surface:

- [`factories/DESIGN.md`](./factories/DESIGN.md) §7 — infrastructure keys
- [`services/DESIGN.md`](./services/DESIGN.md) §7 — per-service keys
- [`machine_learning/DESIGN.md`](./machine_learning/DESIGN.md) §7 — weights and thresholds

---

## 8. Implemented capabilities

- ✅ Four-layer separation with a physical folder boundary
- ✅ `BaseManager` singleton + DI + registry self-registration
- ✅ Parent auto-linking, including deferred resolution
- ✅ `BaseInstanceManager` for multi-instance Radarr/Sonarr
- ✅ `ComponentManagerMixin` declarative subcomponent loading
- ✅ Uniform `prepare()` / `run()` lifecycle
- ✅ Brain-purity enforcement at commit time
- ✅ Per-service failure isolation in `Main.run()`

## 9. Planned additions

| # | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| P1 | **Service-purity hook** — AST-detect scoring/threshold literals and `machine_learning.scoring` re-implementations inside `services/` | Closes the I3 enforcement gap; currently review-only | M | [`hooks/`](../hooks/DESIGN.md) |
| P2 | **Populate `load_summary` for pre-`prepare()` components** | Removes the `instance_manager❌` / `radarr_cache❌` false negatives | S | — |
| P3 | **Warn on unresolved parent link** after deferred retry | Converts silent divergence (§6 row 2) into a visible warning | S | — |
| P4 | **Validate `_infer_parent_from_path` against the registry** at startup and log mismatches | Converts §6 row 3 from silent to detected | S | P3 |
| P5 | **Manager-tree dump command** — render the live tree with parent links and dependency identity | Makes divergence inspectable on demand | S | `registry/cli.py` |
| P6 | **Typed contracts** (dataclasses / TypedDict) at the service↔brain seam, checked in CI | Catches shape drift at the boundary that matters most | M | `contracts/` |
| P7 | **Lifecycle hooks** (`on_prepare_complete`, `on_run_error`) | Enables uniform metrics + web streaming without editing every manager | M | P8 |
| P8 | **Structured event bus** replacing ad-hoc `run_stats` cache keys | Foundation for the web layer's live run view | M | [`factories/web/`](./factories/web/DESIGN.md) |
| P9 | **Explicit `parent_name` everywhere**, deprecating path inference | Removes a whole silent-failure class | M | P4 |
| P10 | **`Protocol`-based interfaces** for the manager contract | Makes `prepare`/`run` conformance statically checkable | M | P6 |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Should an unresolved parent link be fatal rather than a warning? Fatal is safer but risks aborting runs on a benign ordering change. | P3 |
| Q2 | Is `_infer_parent_from_path` worth keeping once P9 lands, or removed outright? | P9 |
| Q3 | Should `orchestration/` absorb the cross-service sequencing currently inline in `Main.run()`? | — |
| Q4 | Do multi-instance managers need per-instance `load_summary`, or is aggregate correct? | P2 |

## 11. Related designs

- [`scripts/DESIGN.md`](../DESIGN.md) — top-level architecture and run order
- [`factories/base_manager.md`](./factories/base_manager.md) — the base class in detail
- [`factories/mixins/component_manager.md`](./factories/mixins/component_manager.md)
- [`machine_learning/ARCHITECTURE.md`](./machine_learning/ARCHITECTURE.md)
- [`support/manager_process_flow_template.txt`](../support/manager_process_flow_template.txt) — the reusable process-flow prompt this layering came from
