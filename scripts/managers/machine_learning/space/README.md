# space

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **space**

**Package** — `scripts.managers.machine_learning.space`
**Run position** — Called during Radarr/Sonarr phases and by the Phase-2.5 coordinator.
**One-liner** — Every disk-space decision: the gating band, the tightness knob, and the delete / downgrade / upgrade / JIT planners — with deletion held as the genuine last resort behind a double consent gate.

---

## Purpose

Finite disk against an unbounded wishlist. This package answers three questions:

| Question | Owner |
|---|---|
| *When* should we reclaim? | [`space_targets.py`](./space_targets.py) — the `(T, U)` band |
| *How selective* should acquisition be right now? | [`tightness.py`](./tightness.py) — `t ∈ [0,1]` |
| *What* should shrink or go? | the planners |

The governing policy, stated in-source: **deletion is the true last resort.**
With `space_exhaustive_downgrade` on (the default), *everything* that can still
be downgraded is downgraded before anything is deleted, and the delete pools
accept only items already at or below the 720p floor.

---

## Script inventory

| Script | Role | Status |
|---|---|---|
| [`space_targets.py`](./space_targets.py) | **Single source of truth for space gating.** `(T, U)` band, `deletions_enabled`, `deletions_consented`, `deletions_disabled_reason`, `exhaustive_downgrade`, `downgrade_regrab_cap`, `coordinator_owns_deletion` | ✅ Implemented |
| [`tightness.py`](./tightness.py) | Acquisition tightness `t` + Schmitt-trigger hysteresis | ✅ Implemented |
| [`delete_planner.py`](./delete_planner.py) | Ranked delete candidates | ✅ Implemented |
| [`downgrade_planner.py`](./downgrade_planner.py) | Step-down planning | ✅ Implemented |
| [`upgrade_planner.py`](./upgrade_planner.py) | Upgrade planning | ✅ Implemented |
| [`jit_planner.py`](./jit_planner.py) | Just-in-time per-episode planning | ✅ Implemented |
| [`coordinator_ranker.py`](./coordinator_ranker.py) | Unified movie+TV pool ranking | ✅ Implemented |
| [`cross_instance_dedup.py`](./cross_instance_dedup.py) | Duplicate detection across instances | ✅ Implemented |
| [`dual_version.py`](./dual_version.py) | Dual-version (HD+4K) handling | ✅ Implemented |
| [`routing_targets.py`](./routing_targets.py) | Routing target resolution | ✅ Implemented |
| [`universe_quality.py`](./universe_quality.py) | Universe-class quality handling | ✅ Implemented |

## Test coverage

15 test modules, including [`test_delete_planner.py`](./test_delete_planner.py),
[`test_downgrade_planner.py`](./test_downgrade_planner.py),
[`test_tightness.py`](./test_tightness.py),
[`test_unscored_deferral.py`](./test_unscored_deferral.py),
[`test_cross_instance_dedup.py`](./test_cross_instance_dedup.py),
[`test_dual_version.py`](./test_dual_version.py).

---

## The space band

One user setting drives every gate, replacing scattered 25/50/100 GB constants:

```
T = free_space_limit                            # floor to keep free
U = T × (1 + space_pressure_headroom_ratio)     # top of the pressure band  (default 0.10)

free ≥ U      → comfortable   — upgrades/acquisition allowed, no reclamation
T ≤ free < U  → pressure band — hold steady (the deletion loop stops here = hysteresis)
free < T      → below floor   — downgrade + delete to reclaim back up to U
```

When `free_space_limit` is unset, `T` defaults to **25 % of the total drive**
(`PRESSURE_FALLBACK_FRACTION`) so the gate scales with the disk. The `25.0` GB
constant is a last resort for when the drive total is *also* unreadable.

---

## Acquisition tightness

A separate, **wider** band than the deletion one — acquisition gets selective at
~30 % above the floor, *before* deletion territory at ~10 %.

```
t = 0   when free ≥ floor × 1.30      comfortable
t = 1   at or below the floor
        linear ramp between

demand-aware ranker:  priority = watchability × demand^t
```

So demand is inert when roomy and dominates at the floor — a shrinking budget
fills with media many people will watch.

`tightness_with_hysteresis` is a Schmitt trigger: tightening **engages** below
`floor × 1.30` but only **releases** above `floor × 1.40`, so a download
finishing at the band edge cannot flip the mode back and forth.

---

## The deletion gate

Deletion requires **both** conditions. Default is off.

| Gate | Meaning |
|---|---|
| `deletions_consented` | Explicit operator opt-in — onboarding's "Media deletion" step, or `RECOMMENDARR_DELETIONS_CONSENT` / `GLIDEARR_DELETIONS_CONSENT` |
| `free_space_limit > 0` | An operator-set floor saying *when* to reclaim |

> *"The floor says **when** to reclaim space; consent says **whether** deletion is
> permitted at all."*

Downgrades, monitoring, grace **marking**, playlist planning and acquisition are
all unaffected. Only the destructive delete APPLY is gated.

---

## Navigation

- **Up:** [`machine_learning/`](../README.md) · **Design:** [`DESIGN.md`](./DESIGN.md)
- **Consumers:** [`services/radarr/storage/`](../../services/radarr/storage/README.md) · [`services/sonarr/storage/`](../../services/sonarr/storage/README.md) · [`services/coordinator/`](../../services/coordinator/README.md)
- **Related:** [`lifecycle/`](../lifecycle/) · [`sizing/`](../sizing/) · [`scoring/`](../scoring/README.md)
