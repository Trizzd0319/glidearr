# sonarr/series/space_pressure.py — verification notes

> Companion to [`DESIGN.md`](./DESIGN.md) and
> [`VERIFICATION_quality.md`](./VERIFICATION_quality.md). Records what a read of
> `space_pressure.py`'s first ~210 lines established, session 42.

---

## 1. ✅ `GLD-SER-05` complete — all three specs route

```python
def _score_ceiling(self) -> float:
    ceiling = float((self.config or {}).get("tv_space_pressure_score_ceiling", self.DEFAULT_SCORE_CEILING))
    return get_threshold("tv_delete_ceiling", self.config, ceiling, logger=...)
```

With `series_monitor` and `series_demote` confirmed in
[`VERIFICATION_quality.md`](./VERIFICATION_quality.md) §1, **all three Sonarr
`THRESHOLD_SPECS` consumers route through the registry.** The calibration
machinery covers TV completely.

The re-anchoring rationale is stated a **third** time, independently:

> 17, not 20: re-anchored with the whole delete family when Group D v2 replaced a
> near-constant +12 bonus with a transcode-risk penalty and translated the score
> axis (file-owning series median 21 → 8).

---

## 2. 🎯 The canonical `dry_run` resolution — and it **raises**

```python
# Resolve dry_run robustly — this manager PUTs to Sonarr, so never default to
# False silently (the dry_run-propagation footgun). Walk kwargs → parent →
# SonarrManager → Main; raise if unresolvable.
_dry_run = kwargs.get("dry_run")
if _dry_run is None:
    _dry_run = getattr(manager, "dry_run", None)
for _root_name in ("SonarrManager", "Main"):
    if _dry_run is not None: break
    ... self.registry.get("manager", _root_name) ...
if _dry_run is None:
    raise ValueError(
        f"❌ {self.__class__.__name__} could not resolve dry_run … Refusing to "
        f"initialize without an explicit value to prevent accidental live profile changes."
    )
```

**Four levels, and it refuses to construct rather than assume.** This is stronger
than [`coordinator/`](../../coordinator/DESIGN.md) §3.6, which I previously called
the strong form.

The full ladder across the repo:

| Tier | Manager | Resolution | On failure |
|---|---|---|---|
| **Canonical** | `sonarr/series/space_pressure.py` | kwargs → parent → `SonarrManager` → `Main` | **raises** |
| Strong | `coordinator/` | kwargs → parent → `Main` + `backup_gate` | *"never silently default"* |
| Weak | `writeback/`, `calendar/` | kwargs → parent | **`False`** = live |

It also names the failure: *"the dry_run-propagation footgun"* — the same bug
`sonarr/__init__.py` records inline (*"episode-file ops … ran LIVE even in
dry_run=True sessions"*).

So `GLD-WB-03` and `GLD-CAL-01` re-scope: there is a **canonical implementation to
copy**, and it is better than the one I cited. `GLD-SP-01`.

---

## 3. 🔴 A third `PRESSURE_FALLBACK_GB` — and the coordinator is the outlier

```python
PRESSURE_FALLBACK_GB = 25.0  # last-resort floor only
```

| Location | Value |
|---|---|
| `machine_learning/space/space_targets.py` | **25.0** |
| `sonarr/series/space_pressure.py` | **25.0** |
| `services/coordinator/space_coordinator.py` | **1000.0** |

Two of three agree at 25. `GLD-COORD-01` was framed as *"two constants, one name,
40× apart"* — it is sharper than that: the coordinator is a **lone outlier**
against a consistent convention, in the manager with the largest blast radius.

---

## 4. 🎯 A documented shipped bug: TV step-down reclaim was **phantom**

> \*arr **never downgrades an existing file** (a file above the new profile's
> cutoff → "cutoff met" → every release rejected), so the old live path — PUT the
> series profile + SeriesSearch and hope — **reclaimed NOTHING (TV step-down space
> was phantom)**.

TV downgrades were reporting reclaim that never occurred. The space plan believed
it had freed GB it had not.

**This is the origin of a distinction documented two folders away.**
[`coordinator/DESIGN.md`](../../coordinator/DESIGN.md) §2 records
*"Stage-1 downgrades only **project** reclaim; Stage 2 **realizes** it"* — the
project/realize split exists **because of this bug**, and neither doc says so.

The fix mirrors the movie path at **episode-file granularity**: profile PUT →
per-file interactive `release?episodeId=` search → pick found ⇒ `DELETE
episodefile/{fid}` then grab the guid → grab error ⇒ post-delete blind
`EpisodeSearch` (*"effective once the file is gone, since the cutoff-met blocker
died with it"*) → **no pick ⇒ file KEPT**.

That last branch is the important one:

> a title is **never traded for an empty indexer result** — profile stays lowered,
> counted `no_release`, re-probes next run

Delete-then-hope would have been the obvious implementation and would lose files
to a quiet indexer. `GLD-SP-04`.

---

## 5. 🟡 Sonarr imports from Radarr — an undeclared cross-service dependency

```python
from scripts.managers.services.radarr.quality.space_pressure import RadarrSpacePressureManager
_pick_stepdown_release = RadarrSpacePressureManager._pick_stepdown_release
```

> The step-down release picker is SHARED with the movie/universe realize paths —
> **imported, not copied** (same ladder semantics …)

The *intent* is right and it is exactly the anti-P-E discipline the register keeps
asking for. But [`ARCHITECTURE.md`](../../../machine_learning/ARCHITECTURE.md)
rule 3 declares `services → contracts` and `services → ml.*` — it says nothing
about `services → services`.

So this is a real cross-service dependency that no stated rule permits or forbids,
and `brain_purity` does not see it (it guards the brain, not the services).

Two further wrinkles: it reaches a **private** method (`_pick_stepdown_release`)
across a service boundary, and it binds it at **module import time**, so importing
Sonarr's space pressure imports Radarr's manager.

The clean home for shared ladder semantics is
[`machine_learning/space/`](../../../machine_learning/space/README.md), which both
services may import by rule. `GLD-SP-03`.

---

## 6. Second confirmed no-op `run()` — and this one names its caller

```python
def run(self):
    # No-op for the SonarrSeriesManager component-iteration; the downgrade pass
    # is driven by the orchestration (run_space_pressure_downgrades) AFTER
    # refresh_scores so it operates on fresh watchability scores.
    return {}
```

Both `quality` and `space_pressure` — the two 40 KB modules — have no-op `run()`
methods. `SonarrSeriesManager.run()` logs `✅ Ran:` for both while nothing happens.

But this one **names its caller**: `orchestration.run_space_pressure_downgrades`,
and states the ordering constraint (*after* `refresh_scores`). That is the lead
`GLD-SERQ-05` needs, and it makes the no-op deliberate rather than incidental —
the component-iteration protocol simply is not the right trigger for a pass that
must run at a specific point in the phase order.

Worth recording as the *reason* the pattern exists, rather than treating every
no-op `run()` as a defect.

---

## 7. Smaller findings

| Finding | Note |
|---|---|
| **Fourth shim caller** | `exhaustive_downgrade`, `downgrade_regrab_cap`, `space_targets` all from `support/utilities/space_targets` — after `coordinator`, `orchestration/series.py`, `series/quality.py` |
| `alert_unconfigured_floor` called here too | Second confirmed call site — reinforces `GLD-SERQ-02` |
| `STEPDOWN_MIN_RELEASE_BYTES = 50 MiB` vs movies' 300 MiB | *"a legit 720p episode can be far smaller"* — a justified divergence, not a copy error |
| `tv_downgrade_realize_cap` = 15 | Bounds slow interactive searches per pass; over-cap files *"re-qualify next run while still oversized (the planner re-picks their series from **file resolutions, not the profile**)"* |
| `SEARCH_CHUNK = 100` | Blind-search fallback chunking, mirroring `quality.py`'s `_BATCH = 200` |
| Recently-**aired** guard (30 d) | *"no Radarr analog"* — TV-specific, correctly identified as such |
| U-target loop | Downgrades *just enough* lowest-score-first to project free ≥ U, *"rather than downgrading the whole low-value catalog at once (avoids a re-grab storm on a large TV library)"* |

---

## 8. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-SP-01` | 🎯 **Adopt this file's `dry_run` resolution as canonical** — four levels (kwargs → parent → `SonarrManager` → `Main`) that **raise** rather than default. **Re-scopes `GLD-WB-03` and `GLD-CAL-01`**: there is a better implementation to copy than `coordinator/`'s | S | `GLD-WB-03`, `GLD-CAL-01`, `GLD-ORCH-01` |
| `GLD-SP-03` | 🟡 **Move `_pick_stepdown_release` into `machine_learning/space/`** — Sonarr importing a **private** method from a Radarr manager at module-import time is a cross-service dependency no stated rule covers, and `brain_purity` cannot see it | S | `GLD-MGR-01` |
| `GLD-SP-04` | **Record the phantom-reclaim bug in `space/` and `coordinator/`** — *"TV step-down space was phantom"* is the **origin** of the project-vs-realize split, and neither doc says so | S | `GLD-COORD-04`, `GLD-SPA-05` |
| `GLD-SP-05` | **Report `no_release` and `deferred` counts** — a file kept because no smaller release existed, and files over `tv_downgrade_realize_cap`, are both real reclaim shortfalls | S | `GLD-COORD-05` |
| `GLD-SP-06` | **Document the no-op-`run()` convention** — a pass with a phase-order constraint cannot use component-iteration as its trigger. Makes the pattern legible instead of looking like five separate defects | S | `GLD-SERQ-01` |
| `GLD-SP-07` | **Read the remaining ~30 KB** — the planner loop, realize path and stamping | M | `GLD-SERQ-04` |

## 9. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Should `PRESSURE_FALLBACK_GB` be a single shared constant? Two of three sites already agree at 25 | `GLD-COORD-01` |
| Q2 | Are there other `services → services` imports? | `GLD-SP-03` |
| Q3 | How often does `no_release` fire — i.e. how much projected TV reclaim never realizes? | `GLD-SP-05` |

**Q3 is the empirical half of D24 for TV.** The `no_release` branch is correct —
a title is never traded for an empty indexer result — but every occurrence is
projected reclaim that does not arrive. If it fires often, TV downgrade projections
systematically overstate, which is a milder version of the very bug §4 records
being fixed.

## 10. Related

- [`VERIFICATION_quality.md`](./VERIFICATION_quality.md) — the sibling 40 KB module
- [`coordinator/DESIGN.md`](../../coordinator/DESIGN.md) §2 — the project/realize split §4 explains the origin of
- [`machine_learning/space/DESIGN.md`](../../../machine_learning/space/DESIGN.md) — `exhaustive_downgrade`, `downgrade_regrab_cap`, the `(T, U)` band
- [`radarr/quality/space_pressure.py`](../../radarr/quality/) — the shared picker's home
