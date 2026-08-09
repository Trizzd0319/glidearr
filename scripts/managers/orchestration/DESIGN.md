# orchestration — Design

> Breadcrumb: [glidearr](../../..) › [scripts](../../README.md) › [managers](../README.md) › **orchestration**

**Package** — `scripts.managers.orchestration`
**Status** — ⚪ Speculative — empty placeholder, nothing implemented
**Related** — [README.md](./README.md) · [`services/coordinator/`](../services/coordinator/README.md)

---

## 1. Problem statement

The original architecture (see
[`support/manager_process_flow_template.txt`](../../support/manager_process_flow_template.txt))
established a strict rule: **submodules never reach sideways into another
service's managers.** Only a coordinating layer may compose across services.

That rule is correct and still holds. But it creates a question the template
answered only conditionally — *where* does cross-service logic live? The template
said "build orchestration helpers under `main.orchestration` if flow grows
complex," and this folder was created in anticipation.

The flow did grow complex. The answer, in practice, was **not** this folder:

- **Run sequencing** stayed inline in `Main.run()`, where the ordering constraints
  (Sonarr before Radarr to overlap the prefetch; SpaceCoordinator at 2.5 so its
  deletes reach the ledger before `PlanSummary` reads it) are visible in one place.
- **Cross-service decisions** went to
  [`services/coordinator/`](../services/coordinator/README.md), which grew into a
  real subsystem with `SpaceCoordinatorManager`, hybrid universe acquisition and
  saga retention.

So this folder documents a design fork that was taken elsewhere. The remaining
question is whether anything should live here at all.

---

## 2. Design goals & non-goals

### Goals (as originally intended)

| # | Goal |
|---|---|
| G1 | Centralise flows that span two or more services. |
| G2 | Keep service submodules free of sideways calls. |
| G3 | Keep `Main` readable as the run declares more phases. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Replacing `services/coordinator/` | That subsystem works and owns cross-service *decisions*. |
| N2 | A workflow engine | Sequencing is short and explicit. |

---

## 3. Architecture

### 3.1 Current state

```
managers/orchestration/
   └── dry_run.py     0 bytes
   (no __init__.py — not importable)
```

### 3.2 The actual split today

```
Main.run()                       ← sequencing, ordering constraints, phase gating
    │
    ├── service managers         ← domain work, no sideways calls (G2 holds)
    │
    └── services/coordinator/    ← cross-service DECISIONS
            SpaceCoordinatorManager
            hybrid_universe_acquisition
            saga_retention_producer
```

G2 is satisfied. G1 is satisfied by `coordinator/`. G3 is **partially** satisfied
— `Main.run()` is long, but its length is mostly explicit phase sequencing that
benefits from being visible in one file.

### 3.3 The one genuine gap: `dry_run`

The empty filename is the interesting part. `dry_run` is currently handled by
propagating `self.dry_run` into every manager individually, and each manager is
independently responsible for honouring it before any APPLY.

That is a **distributed invariant with no central enforcement**. Invariant I6 in
[`scripts/DESIGN.md`](../DESIGN.md) says `dry_run=True` performs zero APPLY, but
nothing verifies it. A new manager that forgets the check would delete during a
dry run, and the only detection is noticing real deletions in a rehearsal.

That is precisely what a `dry_run.py` helper would address — and it is the single
strongest argument for this folder continuing to exist.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Cross-service *decisions* live in `services/coordinator/` | They need service context (quality ladders, tag semantics, space state) that pure orchestration would have to re-import wholesale | This folder |
| D2 | Sequencing stays inline in `Main.run()` | The ordering constraints are load-bearing and are best read as one linear narrative | Extract to an orchestrator class |
| D3 | `dry_run` propagated per-manager | Simple, explicit, works today | Central gate |
| D4 | The folder was kept rather than deleted | It names an unrealized idea that still has one strong use case (§3.3) | Delete |

---

## 5. Invariants

Nothing here executes, so this package has no runtime invariants.

The invariant it *would* enforce, currently unenforced anywhere:

| # | Invariant |
|---|---|
| I1 | `dry_run=True` performs zero APPLY across every service. Currently honoured by convention in each manager, verified nowhere. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius |
|---|---|---|---|
| Import attempt | `ModuleNotFoundError` | No `__init__.py` | Nothing imports it |
| **A manager forgets its `dry_run` check** | **None** | Real APPLY during a rehearsal — deletions, tag changes, grabs | 🔴 Destructive, silent |

Row 2 is the whole reason to keep this folder. It is not a current bug; it is an
unguarded class of bug.

---

## 7. Configuration surface

None.

---

## 8. Implemented capabilities

None. [`dry_run.py`](./dry_run.py) is 0 bytes and there is no `__init__.py`.

## 9. Planned additions

| # | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| P1 | **Central dry-run gate** — a `DryRunGuard` every APPLY path must call, raising if `dry_run` is set and the caller did not declare the mutation | Turns I1 from convention into enforcement (§6 row 2) | M | Audit of APPLY sites |
| P2 | **`dry_run` audit hook** — record every suppressed APPLY into the ledger so a dry run reports exactly what it *would* have done, per service | Strengthens goal G3 in [`scripts/DESIGN.md`](../DESIGN.md): the plan becomes complete rather than inferred | M | P1 |
| P3 | **AST guard: every APPLY method checks `dry_run`** | Same enforcement style as [`hooks/brain_purity.py`](../../hooks/brain_purity.py) — mechanical, not review-dependent | M | P1, [`hooks/`](../../hooks/DESIGN.md) |
| P4 | **Extract `Main.run()` phase sequencing** into a declarative phase list with explicit ordering constraints | Makes the "why is Sonarr before Radarr" rationale machine-readable and testable rather than a comment | M | — |
| P5 | **Add `__init__.py`** so the folder is importable | Prerequisite for anything above | S | — |
| P6 | **Delete the folder** if P1–P4 land in `coordinator/` or `hooks/` instead | An empty folder that stays empty is a false signal about where to put things | S | Decision |

**P1–P3 are one idea at three strengths**, and it is the strongest candidate work
in this document. A dry run that silently applies is the worst failure this
system can have, and it is currently prevented only by every manager remembering.

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Should the dry-run gate live here, or in `factories/` alongside the other cross-cutting infrastructure? `factories/` is arguably the better home — it is infrastructure with no media policy. | P1 |
| Q2 | Should `Main.run()`'s sequencing become declarative (P4), or is inline sequencing genuinely clearer for constraints this specific? | P4 |
| Q3 | If P1 lands in `factories/` and P4 is rejected, should this folder be deleted (P6)? | P6 |
| Q4 | Is there any cross-service concern that belongs here rather than in `coordinator/`? So far, no candidate has appeared. | — |

**Q1 is the decision that determines whether this folder survives.** If the
dry-run gate is infrastructure (it is), it belongs in `factories/`, and this
folder has no remaining purpose.

## 11. Related designs

- [`support/manager_process_flow_template.txt`](../../support/manager_process_flow_template.txt) — the template that proposed this folder
- [`services/coordinator/DESIGN.md`](../services/coordinator/DESIGN.md) — where cross-service decisions actually live
- [`scripts/DESIGN.md`](../DESIGN.md) §5 I6 — the dry-run invariant
- [`hooks/DESIGN.md`](../../hooks/DESIGN.md) — the enforcement pattern P3 would follow
