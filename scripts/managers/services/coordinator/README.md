# coordinator

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [services](../README.md) › **coordinator**

**Manager** — `SpaceCoordinatorManager`
**Run position** — **Phase 2.5**, after Radarr and Sonarr, before PlanSummary.
**One-liner** — The cross-service space capstone: each service owns its own upgrades and downgrades, but **deletion is unified** into one ranked movie+TV pool so the least-valuable bytes go first regardless of which service owns them.

---

## Purpose

From [`space_coordinator.py`](./space_coordinator.py):

> The four space-management phases culminate here. Radarr and Sonarr each own
> their *upgrade* and *downgrade* stages, but **deletion is unified**: when the
> shared media mount drops below the pressure band, movies and TV episodes
> compete in **one ranked pool** sorted by watchability so the least-valuable
> bytes go first — regardless of which service owns them.

Without it, each service reclaims against its own view and a 40 GB unwatched film
survives while a 2 GB rewatched episode is deleted, purely because they are
managed by different applications.

---

## Script inventory

| Script | Size | Role | Tests |
|---|---|---|---|
| [`space_coordinator.py`](./space_coordinator.py) | **72.4 KB** | The capstone — the largest module in the repo | ✅ **61 KB** across 6 files |
| [`hybrid_universe_acquisition.py`](./hybrid_universe_acquisition.py) | 18.8 KB | Universe-aware acquisition | ✅ 9.5 KB |
| [`saga_retention_producer.py`](./saga_retention_producer.py) | 14.8 KB | Saga retention candidates | ✅ 6.9 KB |
| [`__init__.py`](./__init__.py) | 0.5 KB | Package exports | — |

### Design notes in this folder

[`universe_acquisition.md`](./universe_acquisition.md) (26.3 KB) ·
[`this_week_in_history.md`](./this_week_in_history.md) (20.9 KB) ·
[`catchup_retention.md`](./catchup_retention.md) (17.2 KB) ·
[`space_coordinator.md`](./space_coordinator.md) (11.9 KB) ·
[`tv_franchise_discovery.md`](./tv_franchise_discovery.md) (11.3 KB) ·
[`demand_aware_acquisition.md`](./demand_aware_acquisition.md) (8.7 KB)

---

## The pipeline

```
GATE     space_coordinator_enabled AND free_space_limit > 0
         Bail immediately if free ≥ U — "a healthy library is never touched"
              │
STAGE 1  BOTH downgrade passes (Radarr → 720p, Sonarr → 720p)
         Non-destructive, restorable re-grabs → run THROUGHOUT the band
              │
         Re-read free.  free ≥ T ?  ──── yes ──► STOP
              │                              (floor-gated for hysteresis)
              no
              ▼
STAGE 2  Combined delete pool: Radarr movies + Sonarr episodes
         Sort: watchability ASC → critic rating → size DESC
         Accumulate from the bottom until projected free reaches U
         Split back per service; delete via moviefile/{id} · episodefile/{id}
              │
STAGE 3  Restore anything coordinator-deleted whose score has recovered
```

**Two different triggers.** Downgrades run whenever `free < U`; deletion only when
`free < T`. See [`DESIGN.md`](./DESIGN.md) §3.1 — this refines what
[`machine_learning/space/DESIGN.md`](../../machine_learning/space/DESIGN.md) §3.1
says about the pressure band.

---

## Deletion is the true backstop

> Stage-1 downgrades only **project** reclaim; Stage 2 **realizes** it.

With `space_exhaustive_downgrade` on (the default):

> Stage 1 plans **EVERY** title above the 720p floor down to it, and each
> service's `build_delete_candidates` admits a title **ONLY** once it is at/below
> that floor. Stage 2 can therefore only ever fire **after the downgrade pool is
> exhausted**.

Every deletion is tracked, so a later recovery in watchability re-grabs it.

---

## FORK-D — cross-run 4K ledgers

| Key | Contents |
|---|---|
| `radarr/{inst}/pending_4k_evicts` | Rehomed 4K copy awaiting eviction |
| `radarr/{inst}/space_evicted_4k` | Evicted 4K copies awaiting **space recovery** to be re-added as a dual-version bonus |

---

## Navigation

- **Design:** [`DESIGN.md`](./DESIGN.md) · [`space_coordinator.md`](./space_coordinator.md)
- **Brain:** [`machine_learning/space/`](../../machine_learning/space/README.md) — `coordinator_ranker`, `routing_targets`, `space_targets`
- **Services:** [`radarr/`](../radarr/README.md) · [`sonarr/`](../sonarr/README.md)
