# orchestration

> Breadcrumb: [glidearr](../../..) › [scripts](../../README.md) › [managers](../README.md) › **orchestration**

**Package** — `scripts.managers.orchestration` — ⚠️ **not currently a package** (no `__init__.py`)
**Run position** — None. Nothing here executes today.
**One-liner** — The intended home for cross-service coordination helpers. Currently an empty placeholder.

---

## Status: ⚪ Speculative

This folder contains exactly one file, [`dry_run.py`](./dry_run.py), which is
**0 bytes**. There is no `__init__.py`, so the directory is not importable as a
package.

Nothing here runs. It is documented because the empty folder is a real signal
about intended architecture, not because it does anything.

---

## Script inventory

| Script | Doc | Role | Status |
|---|---|---|---|
| [`dry_run.py`](./dry_run.py) | — | Empty (0 bytes). Intended: centralised dry-run gating | ⚪ Speculative |
| `__init__.py` | — | **Absent** — folder is not an importable package | 🔵 Planned |

---

## Why this folder exists

[`support/manager_process_flow_template.txt`](../../support/manager_process_flow_template.txt)
— the reusable process-flow template this architecture was designed against —
closes with:

> Build orchestration helpers under `main.orchestration` if flow grows complex.

and lists candidate workflows: storage cleanup, quality-downgrade decisioning,
ML-based deletion targeting.

The flow **has** grown complex. `Main.run()` now sequences ten managers with
documented ordering constraints, and
[`services/coordinator/`](../services/coordinator/README.md) has absorbed the
genuinely cross-service work (`SpaceCoordinatorManager`, hybrid universe
acquisition, saga retention).

So the need was real and was met — just in `coordinator/` rather than here. This
folder is the vestige of the original plan.

---

## Where cross-service coordination actually lives today

| Concern | Actual home |
|---|---|
| Run sequencing, phase ordering | [`main.py`](../../main.py) — inline in `Main.run()` |
| Unified movie+TV delete planning | [`services/coordinator/space_coordinator.py`](../services/coordinator/space_coordinator.py) |
| Cross-service universe acquisition | [`services/coordinator/hybrid_universe_acquisition.py`](../services/coordinator/hybrid_universe_acquisition.py) |
| Saga retention across services | [`services/coordinator/saga_retention_producer.py`](../services/coordinator/saga_retention_producer.py) |
| Dry-run gating | **Distributed** — `self.dry_run` propagated to every manager individually |

---

## Navigation

- **Up:** [`managers/`](../README.md)
- **Design:** [`DESIGN.md`](./DESIGN.md)
- **Where the work went:** [`services/coordinator/`](../services/coordinator/README.md)
- **Origin:** [`support/manager_process_flow_template.txt`](../../support/manager_process_flow_template.txt)
