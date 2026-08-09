# sonarr/series — Design

> Breadcrumb: [glidearr](../../../../..) › [scripts](../../../../README.md) › [managers](../../../README.md) › [services](../../README.md) › [sonarr](../README.md) › **series**

**Manager** — `SonarrSeriesManager`
**Status** — ✅ Live — the work end of the two paths `run()` reaches
**Existing docs** — [`README.md`](./README.md) + a `.md` per module

---

## 1. Position

This is where Sonarr's live path terminates:

```
SonarrManager.run()
  └─ orchestration            (11 constructed, 2 invoked)
       └─ series              orchestration/series.py
            └─ SonarrSeriesManager        ← here
                 ├─ helpers · retrieval · monitoring
                 ├─ quality          40.8 KB
                 ├─ space_pressure   40.2 KB
                 └─ sync
```

`quality.py` and `space_pressure.py` are 81 KB between them and carry three live
`THRESHOLD_SPECS` consumers:

| Spec | Constant | Consumer |
|---|---|---|
| `series_monitor` | 35 | `series/quality.py:463` |
| `series_demote` | 17 | `series/quality.py:472` |
| `tv_delete_ceiling` | 17 | `series/space_pressure.py:159` |

All three are **AXIS V2** — the persisted `watchability_score` column
([`thresholds/DESIGN.md`](../../../machine_learning/thresholds/DESIGN.md) §3.4).
So this folder is where the re-anchored 20 → 17 delete family actually acts on TV.

---

## 2. 🟡 A third component-loading mechanism

```python
self.components = self.load_components(
    component_map={...six...},
    registry_prefix="sonarr.series",
    api_kwarg_name="sonarr_api",
)
```

Sonarr alone now uses **three** approaches:

| Manager | Mechanism | Iterates |
|---|---|---|
| `SonarrManager` | `component_dependencies` + `topo_order` + `split_components` + `_load_component` | the **declared, filtered** set |
| `sync/`, `quality/`, `orchestration/quality.py` | `split_components` + a manual loop | the **full** map |
| **`SonarrSeriesManager`** | **`load_components`** (the `ComponentManagerMixin` method) | **whatever loaded** |

`ComponentManagerMixin` exposes both `split_components` and `load_components`, and
[`plex/README.md`](../../plex/README.md) notes Plex *"uses `split_components(...)`,
**not** `load_components`"* — so the choice is known and made per-manager, but
nothing states when each is appropriate.

Three mechanisms in one service is a maintenance surface: a fix to loading
semantics has to be applied three ways, and `GLD-ORCH-S02`'s soft-disable protocol
would need porting into each. `GLD-SER-02`.

---

## 3. 🟡 A fourth denominator — and this one is *no* denominator

`prepare()` and `run()` both iterate `self.components` and log per-component:

```python
for name in self.components:
    comp = getattr(self, name, None)
    if comp and hasattr(comp, "run"):
        try:    comp.run();  self.logger.log_debug(f"✅ Ran: {name}")
        except: self.logger.log_error(f"❌ Failed to run '{name}': {e}")
```

**There is no aggregate summary line at all.** No `N/M`, no roll-up, no
`log_filtered_component_summary`.

Collecting the four variants found in this service:

| Manager | Summary | Can it reveal a missing component? |
|---|---|---|
| `SonarrManager` | `8/8` against the **filtered** `component_dependencies` | ❌ Structurally cannot (`GLD-SON-13`) |
| `orchestration` | `9/11` against the **full** `orchestrator_map` | ✅ Yes (`GLD-ORCH-S03`) |
| `sync/`, `quality/` | `log_filtered_component_summary` over `split_components` output | ✅ (though both are dead) |
| **`series`** | **none** | ❌ **No summary exists** |

The `if comp and hasattr(comp, "run")` guard means a component that failed to load
is **silently skipped** — the same shape as `GLD-SON-14`, one level down, and here
without even a per-run count to notice it by.

Given this is the manager that owns the TV delete ceiling and the monitor
threshold, a silently-absent `space_pressure` or `quality` would mean TV
space-pressure simply does not happen that run, with nothing but a single
`log_error` to say so. `GLD-SER-01`.

---

## 4. 🟡 "Quality" now names three things

Exactly the collision `GLD-SON-15` records for "sync":

| Path | What it is | State |
|---|---|---|
| `sonarr/quality/` | `SonarrQualityManager` | ❌ **Dead** — superseded copy with a broken loop |
| `sonarr/orchestration/quality.py` | `SonarrOrchestrationQualityManager` | ✅ Live (constructed; not invoked by `run()`) |
| **`sonarr/series/quality.py`** | `SonarrSeriesQualityManager`, **40.8 KB** | ✅ **Live and invoked** |

The third is the one that actually runs, and it is the one a reader is least
likely to find first. Both collisions ("sync", "quality") have now cost a
disambiguating read. `GLD-SER-03`.

---

## 5. 🎯 The tests name behaviours, not modules

| Test | Behaviour pinned |
|---|---|
| `test_exhaustive_downgrade.py` | 8.0 KB — the `space_exhaustive_downgrade` default |
| `test_monitor_by_watchability.py` | 9.4 KB — the `series_monitor` threshold |
| `test_space_pressure_realize.py` | 6.9 KB — realized vs projected reclaim |
| `test_quality_upgrade_target.py` · `test_quality_upgrade_floor.py` | The upgrade ladder's ends |
| `test_active_watcher_gating.py` | Active-watcher upgrade gating |
| `test_space_pressure_floor.py` | The floor |

Seven files, ~34.6 KB against ~90 KB of source. Every name states a *behaviour*
under test rather than the module it lives in — so a reader can tell what is
guaranteed without opening them.

That is a marked contrast with the packages where coverage exists but names the
file (`test_client.py`, `test_build.py`) and with those where the invariant is
named in a docstring and has no test at all
([`playlists/spoiler.py`](../../../machine_learning/playlists/DESIGN.md) §3.4).

Worth citing as the model when `GLD-SON-16`-style test items are worked.

---

## 6. Smaller observations

| Observation | Note |
|---|---|
| `parent_name = "SonarrSeries"` class attr, then `self.parent_name = self.__class__.__name__` | Instance says `SonarrSeriesManager`; `orchestration/series_sync.py` declares `parent_name = "SonarrSeries"` and looks up by it — it survives only because `kwargs["manager"]` wins first |
| `getattr(kwargs.get("manager", {}), "sonarr_cache", None)` | Third use of `{}` as a null-object stand-in (also `orchestration/`, `sonarr/`) |
| `run()` isolates each component | A `quality` failure does not stop `space_pressure` |
| `# ✅ Dual-cache support` | `global_cache` + `sonarr_cache` held side by side |

---

## 7. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-SER-01` | 🟡 **Add a component summary to `prepare()`/`run()`** — there is **none**, and `if comp and hasattr(...)` silently skips a component that failed to load. This manager owns the TV delete ceiling and monitor threshold; a silently-absent `space_pressure` means TV space-pressure does not happen, with one `log_error` to show for it | §3 — fourth denominator variant, and the only one with no denominator *(P-D)* | S | `GLD-SON-13`, `GLD-ORCH-S03` |
| `GLD-SER-02` | 🟡 **Document when to use `load_components` vs `split_components`** — Sonarr uses three loading mechanisms; a semantics fix must be applied three ways, and `GLD-ORCH-S02`'s protocol ported into each | §2 | S | `GLD-MIX-01`, `GLD-ORCH-S02` |
| `GLD-SER-03` | 🟡 **"Quality" names three things** — dead package, live orchestrator, live 40.8 KB series module. Same collision as "sync"; both have now cost a disambiguating read | §4 | S | `GLD-SON-15` |
| `GLD-SER-04` | 🎯 **Cite this test set as the naming model** — every file names a *behaviour*, not a module | §5. Useful whenever a test-coverage item is worked | S | `GLD-PLY-01`, `GLD-CHL-01` |
| `GLD-SER-05` | **Document `quality.py` and `space_pressure.py`** — 81 KB carrying three live `THRESHOLD_SPECS` consumers, all AXIS V2 | The re-anchored 20 → 17 delete family acts on TV here | L | `GLD-THR-01` |
| `GLD-SER-06` | **Verify the `parent_name` mismatch is harmless** — class attr `"SonarrSeries"` vs instance `"SonarrSeriesManager"`, with `series_sync.py` looking up the former | §6; survives only because `kwargs["manager"]` is checked first | S | — |

## 8. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Does `load_components` drop a failed component from its return, or include it as `None`? That decides whether §3's gap is invisible or merely unsummarised | `GLD-SER-01` |
| Q2 | Is there a stated rule for `load_components` vs `split_components`? | `GLD-SER-02` |
| Q3 | Do `quality.py` and `space_pressure.py` read their thresholds through `thresholds/registry.get_threshold`, or as literals? | `GLD-SER-05` |

**Q3 is the one worth checking soon.** `THRESHOLD_SPECS` names these three
consumers by `file:line`, and the whole point of the registry is that a
calibrated value can be swapped in without touching call sites. Whether these two
40 KB modules actually route through `get_threshold` — or still hold `35` and `17`
as literals — determines whether the shadow-mode machinery can ever drive TV.

## 9. Related designs

- [`orchestration/DESIGN.md`](../orchestration/DESIGN.md) §2, §6.5 — the caller
- [`sonarr/DESIGN.md`](../DESIGN.md) §12 — the parent's filtered summary
- [`machine_learning/thresholds/DESIGN.md`](../../../machine_learning/thresholds/DESIGN.md) §3.4 — the AXIS V2 family these three specs belong to
- [`machine_learning/space/DESIGN.md`](../../../machine_learning/space/DESIGN.md) — `space_exhaustive_downgrade`, pinned by `test_exhaustive_downgrade.py`
- [`quality/DESIGN.md`](../quality/DESIGN.md) · [`sync/DESIGN.md`](../sync/DESIGN.md) — the two dead namesakes
