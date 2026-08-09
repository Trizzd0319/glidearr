# sonarr/repair — Design

> Breadcrumb: [glidearr](../../../../..) › [scripts](../../../../README.md) › [managers](../../../README.md) › [services](../../README.md) › [sonarr](../README.md) › **repair**

**Manager** — `SonarrRepairManager`
**Status** — ✅ Loaded and critical · 🔴 A `critical_keys` name that matches nothing
**Existing docs** — [`README.md`](./README.md) + a `.md` for **every** module

---

## 1. Shape

15 components, 123 KB, and **a `.md` for every single module** — the most
complete per-module documentation in the repo. **Zero tests.**

`repair` is in `SonarrManager.component_dependencies` and `critical_keys`, so it
loads and runs.

---

## 2. 🔴 `critical_keys` declares `"cache"`; the component is `"repair_cache"`

```python
all_component_classes = {
    "anomaly": …, "repair_cache": …, "episodes": …, "file": …, "filepaths": …,
    "history": …, "instance": …, "metadata": …, "monitoring": …, "orphans": …,
    "quality": …, "series": …, "storage": …, "tags": …, "validator": …,
}                                    # 15 keys — note "repair_cache"

critical_keys = {
    "cache",        # ← matches NOTHING
    "filepaths", "instance", "monitoring", "storage", "validator",
}
```

`split_components` partitions with `if k in critical_keys`, so:

- **`"cache"` matches nothing** and contributes no component.
- **`SonarrRepairCacheManager` falls to the non-critical path**, because
  `"repair_cache"` is not in `critical_keys`.

Two consequences. A `repair_cache` failure no longer sets
`all_critical_loaded = False`, so `sonarr.repair_manager_initialized` stays
`True`. And, being non-critical, it is now subject to the `parent_name` filter —
so it is also a `GLD-SPLIT-02` candidate for being dropped entirely.

**This is the second instance of the same shape.**
[`sonarr/DESIGN.md`](../DESIGN.md) §12.2 found `critical_keys` declaring
`"quality"` while `quality` was filtered out of the loadable set. Same class of
bug, different mechanism — there a filtered component, here a **misspelt key**.

`GLD-SON-12` proposed asserting `critical_keys ⊆ all_component_classes`. It now
has two confirmed instances and is no longer a hypothetical. `GLD-REP-01`.

---

## 3. 🎯 Both sides of the `parent_name` match are hand-forced

This one file carries **two** workarounds for `split_components`, and together
they explain `GLD-SPLIT-02` far better than the `episodes/` patch did.

**Forcing the component side:**

```python
# Give sub-managers an explicit parent_name so split_components can
# match them correctly (BaseManager's path inference yields "SonarrRepair",
# which would never equal the parent_name_match below).
"parent_name": self.__class__.__name__,
```

**Forcing the manager side:**

```python
# Use __class__.__name__ rather than self.parent_name: BaseManager
# overwrites self.parent_name with the caller's init_args value
# ("SonarrManager"), making the match impossible for repair sub-managers.
parent_name_match=self.__class__.__name__,
```

### 3.1 `parent_name` has at least three sources

| Source | Example value here |
|---|---|
| Class attribute | `"SonarrManager"` |
| `BaseManager` **path inference** | `"SonarrRepair"` |
| `BaseManager` overwrite from `init_args` | `"SonarrManager"` |
| Explicit `self.parent_name = …` in `__init__` | `"SonarrRepairManager"` |

`split_components` matches on whichever wins, and the precedence is not stated
anywhere. That is why both sides needed forcing.

### 3.2 And it means four different things across four managers

| Manager | Class attr | Semantic |
|---|---|---|
| `SonarrSeriesManager` | `"SonarrSeries"` | own name **minus** `Manager` |
| `SonarrStorageManager` | `"SonarrStorageManager"` | own **full** name |
| `SonarrEpisodesShardingManager` | `"SonarrEpisodes"` | **parent's** name minus `Manager` |
| **`SonarrRepairManager`** | **`"SonarrManager"`** | **parent's full name** |

Four managers, four semantics, one attribute — and a filter that compares them
for equality.

This is the root cause behind `GLD-SPLIT-02`, `GLD-EPI-06` and the `sharding`
workaround. The attribute is not a stable identity; it is whatever the last
writer set, under a convention that is different in every class that declares it.
`GLD-REP-02`.

---

## 4. 🟡 `GLD-SPLIT-01` shapes the `init_kwargs` contract

```python
# Pass through the API + instance refs so sub-managers can resolve
# their dependencies without raising during split_components introspection.
"sonarr_api":       kwargs.get("sonarr_api") or getattr(kwargs.get("manager"), "sonarr_api", None),
"instance_manager": kwargs.get("instance_manager") or getattr(kwargs.get("manager"), "instance_manager", None),
```

`GLD-SPLIT-01` recorded that `split_components` constructs a throwaway instance
of each non-critical component purely to read `parent_name`. This comment shows
the cost is not only a duplicated constructor call and its side effects — **every
caller must supply enough kwargs for that throwaway construction to succeed.**

So the introspection leaks into the caller's contract: `init_kwargs` is sized not
by what the components need to *work*, but by what they need to not *raise while
being inspected*.

That strengthens `GLD-STO-04`'s question considerably. A helper that (a) is routed
around by four of five callers, (b) drops components on an unstable attribute, and
(c) dictates the shape of every caller's `init_kwargs` — for the sole purpose of
reading one string — is a candidate for removal rather than repair.

---

## 5. What it gets right

**Per-component registry flags on both branches** — `sonarr.repair.{name}_initialized`,
same as [`storage/`](../storage/DESIGN.md) §7 and better than the managers that
only publish an aggregate.

**`load_summary` carries the exception text** — `f"❌ Failed: {e}"`.

**Non-criticals are isolated** — a failure logs and continues without touching
`all_critical_loaded`.

**A `.md` for all 15 modules.** `anomaly`, `cache`, `episodes`, `file`,
`filepaths`, `history`, `metadata`, `monitoring`, `orphans`, `quality`, `series`,
`storage`, `tags`, `validator` — every one documented. No other folder in the repo
is complete on that axis.

**The workarounds are honest.** Both comments state the cause, the mechanism and
the remedy. They are the reason §3 is legible at all.

---

---

## 5.5 `anomaly.py` — session 51

### 5.5.1 ✅ Q4 answered: this is **not** the LEGACY axis

Sonarr's `anomaly.py` does **no scoring at all**. Three methods, both scans pure
set-differences:

| Method | Compares |
|---|---|
| `scan_for_metadata_anomalies` | live series titles vs cached series titles |
| `identify_orphaned_episodes` | `episodeId`s on files vs defined episode ids |
| `generate_anomaly_report` | combines the two |

No `_score_owned`, no watchability, no demote/restore. **The LEGACY scoring axis
is Radarr-only**, which usefully narrows D27 and `GLD-THR-03` to a single service.
The two files share a name and nothing else.

### 5.5.2 🔴 `SonarrRepairManager` has no `run()` and no `prepare()`

Reading `repair/__init__.py` in full: it defines `__init__` and nothing else.

`repair` **is** in `SonarrManager.component_dependencies` **and** its
`critical_keys`. So:

```python
# SonarrManager.prepare()
if comp and hasattr(comp, "prepare"):   → skipped
# SonarrManager.run()
if component and hasattr(component, "run"):  → skipped, and NO entry in results
```

And because no `results` entry is written, `all_ok = all(results.values())` is
unaffected — [`sonarr/DESIGN.md`](../DESIGN.md) §12.4's `GLD-SON-14`, firing on a
**critical** component.

So `repair` constructs **15 sub-managers** every run, publishes 15 registry flags,
logs a component summary — and then nothing invokes any of it. Same shape as
[`sync/`](../sync/DESIGN.md), except `sync` is at least honestly absent from
`component_dependencies`; this one is *declared critical*.

Unless something reaches in by attribute, as
[`orchestration/quality.py`](../orchestration/DESIGN.md) §2 does with its
`get_*_manager()` accessors. `GLD-REP-07`.

### 5.5.3 🔴 The anomaly scan reads a cache key nothing writes

```python
cache_key = f"sonarr::{instance_name}::series"      # double-colon
cached_series = self.sonarr_cache.get(cache_key) or []
...
missing_in_cache = live_titles - cached_titles
```

`CacheKeyPaths` uses **slash**-separated paths throughout —
`sonarr/<instance>/library`, `sonarr/<instance>/metadata`. Nothing in the registry
produces a `sonarr::<instance>::series` key.

So `cached_series` is `[]`, `cached_titles` is empty, and
`missing_in_cache = live_titles - ∅` = **every series in the library**. The scan
would then log:

```
⚠️ {8000} series missing in cache: {…every title…}
```

— a single warning line containing the entire library.

Moot today because of §5.5.2 (nothing calls it), which is likely why it has never
been seen. It becomes live the moment `GLD-REP-07` is resolved by wiring the
manager up. `GLD-REP-08`.

This is a third distinct cache-key defect after `GLD-STO-01` (unformatted
template) and `GLD-CACHE-S01` (shim vs direct import) — and the only one that
invents a **format**.

### 5.5.4 🟡 A hardcoded `parent_name` that the framework overwrites

```python
def __init__(self, ...):
    self.parent_name = "SonarrRepair"        # line 1
    ...
    super().__init__(logger, config, self.global_cache, validator, registry, **kwargs)
    parent = self.registry.get("manager", self.parent_name)     # ← reads it back
```

`kwargs` carries `parent_name="SonarrRepairManager"`, passed deliberately by the
parent (§3). And the parent's own comment states the mechanism:
*"BaseManager overwrites `self.parent_name` with the caller's `init_args` value."*

If that holds, the hardcoded `"SonarrRepair"` is **dead on arrival** — assigned,
overwritten by `super().__init__`, and never read at its intended value. The
registry lookup two lines later resolves `"SonarrRepairManager"`, and the closing
debug line prints `(Parent: SonarrRepairManager)` rather than the `"SonarrRepair"`
the author wrote.

A **fifth** `parent_name` variant, and the first where a module sets it explicitly
and the framework silently discards the value. Cited from the parent's comment
rather than a read of `BaseManager`. `GLD-REP-09`.

### 5.5.5 An API-surface discrepancy

```python
self.sonarr_api.get_all_sonarr_apis()          # here
self.instance_manager.get_all_sonarr_apis()    # episodes/retrieval.py (the dead stub)
```

The same method name is called on two different objects. Either both expose it, or
one of these raises `AttributeError`. The other caller is the shadowed stub
(`GLD-EPI-01`), so it has never run — leaving this one unverified.
`GLD-REP-10`.

---

## 6. Planned additions

> **§5.6 — Q9 applied, session 53.** §5.5.2 concluded `SonarrRepairManager` is
> *"invoked by nothing"* from it having no `run()`. Checklist Q9 says that is not
> evidence. It is not: **the caller exists.**
>
> [`orchestration/repair.py`](../orchestration/repair.py) exposes **15**
> `run_*_repairs` methods plus a `run_all_repairs` sequencing 14 of them. So the
> finding is not *"nobody wrote a caller"* — somebody wrote a thorough one. It is
> **two-level dormancy**:
>
> ```
> SonarrRepairManager                no run()  ← constructs 15 sub-managers
>   ↑ called by
> SonarrOrchestrationRepairManager             ← has run_all_repairs()
>   ↑ called by
>   … nothing — orchestration.run() invokes only `series` and `episodes`
> ```
>
> **🔴 And the facade calls a method that does not exist.**
> `run_anomaly_repairs` calls `self.repair.anomaly.detect_unexpected_entries()`;
> `SonarrRepairAnomalyManager` defines only `scan_for_metadata_anomalies`,
> `identify_orphaned_episodes` and `generate_anomaly_report`. `run_all_repairs`
> invokes it **untrapped at step 11 of 14**, so `run_monitoring_repairs`,
> `run_history_repairs`, `run_episodes_repairs` and `run_metadata_repairs` are
> never reached and the `✅ All repair operations completed.` line never prints.
> Second instance of *"never-executed code accumulates errors that execution
> would catch"* after [`quality/DESIGN.md`](../quality/DESIGN.md) §2.
>
> **✅ `repair_cache` is the right name.** The facade uses
> `self.repair.repair_cache`, matching `all_component_classes` — so `GLD-REP-01`
> is a **stale key in `critical_keys`**, and the fix is one string.
>
> **🎯 And this sub-orchestrator uses the soft-disable protocol correctly:**
> `self.active = False` + `self._inactive_reason` when `repair` is unavailable —
> while `orchestration/series.py` **raises** for the same class of missing
> dependency. So `GLD-ORCH-S08` is an *inconsistency* finding, not an *absence*
> one, and `repair.py` is the in-repo example to point `series.py` at.

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-REP-11` | 🔴 **`run_anomaly_repairs` calls `detect_unexpected_entries()`, which does not exist** — `run_all_repairs` invokes it untrapped at step 11 of 14, aborting four remaining passes | S | `GLD-REP-07` |
| `GLD-REP-07` | 🔴 **`SonarrRepairManager` has no `run()` and no `prepare()`** — yet `repair` is in `component_dependencies` **and** `critical_keys`. It constructs 15 sub-managers, publishes 15 registry flags, logs a summary, and is then invoked by nothing. `GLD-SON-14` firing on a *critical* component | S | `GLD-SON-14`, `GLD-ORCH-S01` |
| `GLD-REP-08` | 🔴 **`anomaly.py` reads `sonarr::{inst}::series`** — double-colon, a format `CacheKeyPaths` never produces. `cached_series` is always `[]`, so `missing_in_cache` is the **entire library**, logged as one warning line. Dormant only because §5.5.2 means nothing calls it | S | `GLD-REP-07`, `GLD-STO-08` |
| `GLD-REP-09` | 🟡 **`anomaly.py`'s hardcoded `parent_name = "SonarrRepair"` is overwritten by `BaseManager`** before it is read — the registry lookup two lines later resolves `"SonarrRepairManager"`. Fifth `parent_name` variant, and the first where an explicit value is silently discarded | S | `GLD-REP-02` |
| `GLD-REP-10` | **`sonarr_api.get_all_sonarr_apis()` vs `instance_manager.get_all_sonarr_apis()`** — same method, two receivers. The other caller is the shadowed stub, so neither path is verified | S | `GLD-EPI-01` |
| `GLD-REP-01` | 🔴 **`critical_keys` declares `"cache"` but the component is `"repair_cache"`** — the key matches nothing, and `SonarrRepairCacheManager` silently falls to the **non-critical** path (so its failure leaves `sonarr.repair_manager_initialized` `True`, and it becomes a `GLD-SPLIT-02` drop candidate). **Second instance of `GLD-SON-11`'s shape** — `GLD-SON-12`'s assertion is now justified by two live cases | S | `GLD-SON-12`, `GLD-SPLIT-02` |
| `GLD-REP-02` | 🎯 **`parent_name` has three sources and four semantics** — class attr, `BaseManager` path inference (`"SonarrRepair"`), `init_args` overwrite, explicit assignment; meaning own-name / own-full-name / parent-name / parent-full-name depending on the class. **This is the root cause behind `GLD-SPLIT-02`, `GLD-EPI-06` and both workarounds here.** Define one semantic, or stop matching on it | S | `GLD-SPLIT-02`, `GLD-EPI-06` |
| `GLD-REP-03` | **`split_components` introspection dictates every caller's `init_kwargs`** — components must be constructible *while being inspected*, so `init_kwargs` is sized by introspection needs rather than runtime needs. Strengthens the case in `GLD-STO-04` for removing the machinery | S | `GLD-SPLIT-01`, `GLD-STO-04` |
| `GLD-REP-04` | **Add tests** — 123 KB, 15 components, zero test files, on a manager that is `critical` in `SonarrManager` | M | — |
| `GLD-REP-05` | **Read `anomaly.py`** — the Sonarr counterpart to Radarr's LEGACY-axis `repair/anomaly.py`, which D27 is about | S | `GLD-THR-03`, D27 |
| `GLD-REP-06` | **Document the 15 modules** — each has a `.md`; none has a `DESIGN.md` | L | — |

## 7. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Was `"cache"` renamed to `"repair_cache"` without updating `critical_keys`, or was the key always wrong? | `GLD-REP-01` |
| Q2 | Does `repair_cache` actually survive the `parent_name` filter, or is it dropped like `sharding`? | `GLD-REP-01`, `GLD-SPLIT-02` |
| Q3 | What is `parent_name` *for*, once `split_components` stops matching on it? | `GLD-REP-02` |
| Q4 | ✅ **ANSWERED session 51** — **no.** Sonarr's `anomaly.py` does set-differences only (cached vs live titles; file `episodeId`s vs defined ids) with **no scoring whatsoever**. **The LEGACY axis is Radarr-only**, so D27 and `GLD-THR-03` narrow to a single service | ✅ Closed |

**Q2 is the one with teeth.** `repair_cache` is now non-critical *by accident*
(§2) and therefore routed through the filter that silently dropped `sharding`
(§3). If it is also dropped, a component the author considered critical enough to
name in `critical_keys` is **not loaded at all**, and nothing reports it — the
`load_summary` would simply not have a row for it.

That is `GLD-REP-01` and `GLD-SPLIT-02` compounding into a single silent
disappearance, and it is checkable by looking at one run's repair summary.

## 8. Related designs

- [`sonarr/DESIGN.md`](../DESIGN.md) §12.2 — `GLD-SON-11`, the first `critical_keys` mismatch
- [`episodes/DESIGN.md`](../episodes/DESIGN.md) §3, §3.1 — the `sharding` drop and the conditional convention
- [`storage/DESIGN.md`](../storage/DESIGN.md) §4 — the filter is dead in four of five callers
- [`quality/DESIGN.md`](../quality/DESIGN.md) §3 — `GLD-SPLIT-01`, whose cost §4 extends
